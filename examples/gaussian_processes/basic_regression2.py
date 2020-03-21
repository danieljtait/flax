from jax.config import config
config.update("jax_enable_x64", True)

from absl import app
from absl import flags
from absl import logging

from flax import nn
from typing import Callable

import jax
from jax import random, ops
from jax.tree_util import tree_flatten, tree_unflatten
import jax.numpy as jnp

import scipy as oscipy
import kernels
import distributions

FLAGS = flags.FLAGS

flags.DEFINE_bool(
    'plot', default=True,
    help=('Plot the results.', ))


def _diag_shift(mat, val):
    """ Shifts the diagonal of mat by val. """
    return ops.index_update(
        mat,
        jnp.diag_indices(mat.shape[-1], len(mat.shape)),
        jnp.diag(mat) + val)


class MeanShiftDistribution(nn.Module):
    """ Shifts the mean of a distribution with `mean` field. """
    def apply(self, p, shift):
        """ Shift the mean of `p` by `shift`.

        Args:
            p: `dataclass` with field `mean`
            shift: nd-array shift vector, should be broadcastable with
              p.mean

        Returns:
            pnew: A new object of the same type as `p` with
              `pnew.mean = p.mean + shift`.
        """
        try:
            return p.replace(mean=p.mean + shift)
        except AttributeError:
            raise AttributeError('{} must have a `mean` field.'.format(p))


class GaussianProcessLayer(nn.Module):
    """ Given index points the role is to provide
    finite dimensional distributions.

    This implementation handles very little with regards to
    parameterisations.
    """
    def apply(self,
              index_points: jnp.ndarray,
              kernel_fun: Callable,
              mean_fun: Callable = None,
              jitter: float =1e-4):
        """

        Args:
            index_points: the nd-array of index points of the GP model
            kernel_fun: callable kernel function.
            mean_fun: callable mean function of the GP model.
              (default: `None` is equivalent to lambda x: jnp.zeros(x.shape[:-1]))
            jitter: float `jitter` term to add to the diagonal of the covariance
              function before computing Cholesky decompositions.

        Returns:
            p: `distributions.MultivariateNormalTriL` object.
        """
        if mean_fun is None:
            mean_fun = lambda x: jnp.zeros(x.shape[:-1], dtype=index_points.dtype)

        mean = mean_fun(index_points)
        cov = kernel_fun(index_points)
        cov = _diag_shift(cov, jitter)

        return distributions.MultivariateNormalTriL(
            mean, jnp.linalg.cholesky(cov))


class MarginalObservationModel(nn.Module):
    """ The observation model p(y|x, {hyper par}) = ∫p(y,f|x)df where f(x) ~ GP(m(x), k(x, x')). """
    def apply(self, pf: distributions.MultivariateNormalTriL) -> distributions.MultivariateNormalFull:
        """ Applys the marginal observation model of the conditional

        Args:
            pf: distribution of the latent GP to be marginalised over,
              a `distribution.MultivariateNormal` object.

        Returns:
            py: the marginalised distribution of the observations, a
              `distributions.MultivariateNormal` object.
        """
        obs_noise_scale = jax.nn.softplus(
            self.param('observation_noise_scale',
                       (), jax.nn.initializers.ones))

        covariance = pf.scale @ pf.scale.T
        covariance = _diag_shift(covariance, obs_noise_scale**2)

        return distributions.MultivariateNormalFull(
            pf.mean, covariance)


class GPModel(nn.Module):
    """ Model for i.i.d noise observations from a GP with
    RBF kernel. """
    def apply(self, x, dtype=jnp.float64):
        """

        Args:
            x: Index points of the observations.
            dtype: the data-type of the computation (default: float64)

        Returns:
            py_x: Distribution of the observations at the index points.
        """
        kern_fun = kernels.RBFKernelProvider(x, name='kernel_fun')
        pf_x = GaussianProcessLayer(x, kern_fun, name='gp_layer')
        # uncomment to specify a mean function
        # linear_mean = nn.Dense(x, features=1, name='linear_mean',
        #                        dtype = dtype)
        # pf_x = MeanShiftDistribution(
        #     pf_x, linear_mean[..., 0], name='mean_shift')

        py_x = MarginalObservationModel(pf_x, name='observation_model')
        return py_x


def build_par_pack_and_unpack(model):
    """ Build utility functions to pack and unpack paramater pytrees
    for the scipy optimizers. """
    value_flat, value_tree = tree_flatten(model.params)
    section_shapes = [item.shape for item in value_flat]
    section_sizes = jnp.cumsum(jnp.array([item.size for item in value_flat]))

    def par_from_array(arr):
        value_flat = jnp.split(arr, section_sizes)
        value_flat = [x.reshape(s)
                      for x, s in zip(value_flat, section_shapes)]

        params = tree_unflatten(value_tree, value_flat)
        return params

    def array_from_par(params):
        value_flat, value_tree = tree_flatten(params)
        return jnp.concatenate([item.ravel() for item in value_flat])

    return par_from_array, array_from_par


def get_datasets(sim_key: random.PRNGKey, true_obs_noise_scale: float =0.5) -> dict:
    """ Generate the datasets. """
    index_points = jnp.linspace(-3., 3., 25)[..., jnp.newaxis]
    y = (jnp.sin(index_points[:, 0])
         + true_obs_noise_scale * random.normal(sim_key, index_points.shape[:-1]))
    train_ds = {'index_points': index_points, 'y': y}
    return train_ds


def train(train_ds):
    """ Complete training of the GP-Model.

    Args:
        train_ds: Python `dict` with entries `index_points` and `y`.

    Returns:
        trained_model: A `GPModel` instance with trained hyper-parameters.

    """
    rng = random.PRNGKey(0)

    # initialise the model
    py, params = GPModel.init(rng, train_ds['index_points'])
    model = nn.Model(GPModel, params)

    # utility functions for packing and unpacking param dicts
    par_from_array, array_from_par = build_par_pack_and_unpack(model)

    @jax.jit
    def loss_fun(model: GPModel, params: dict) -> float:
        py = model.module.call(params, train_ds['index_points'])
        return -py.log_prob(train_ds['y'])

    # wrap loss fun for scipy.optimize
    def wrapped_loss_fun(arr):
        params = par_from_array(arr)
        return loss_fun(model, params)

    @jax.jit
    def loss_and_grads(x):
        return jax.value_and_grad(wrapped_loss_fun)(x)

    res = oscipy.optimize.minimize(
        loss_and_grads,
        x0=array_from_par(params),
        jac=True,
        method='BFGS')

    logging.info('Optimisation message: {}'.format(res.message))

    trained_model = model.replace(params=par_from_array(res.x))
    return trained_model


def main(_):
    train_ds = get_datasets(random.PRNGKey(123))
    trained_model = train(train_ds)

    if FLAGS.plot:
        import matplotlib.pyplot as plt

        obs_noise_scale = jax.nn.softplus(
            trained_model.params['observation_model']['observation_noise_scale'])

        def learned_kernel_fn(x1, x2):
            return kernels.RBFKernelProvider.call(
                trained_model.params['kernel_fun'], x1)(x1, x2)

        def learned_mean_fn(x):
            return jnp.zeros(x.shape[:-1])
            # return nn.Dense.call(trained_model.params['linear_mean'], x, features=1)[:, 0]

        xx_new = jnp.linspace(-3., 3., 100)[:, None]

        # prior GP model at learned model parameters
        fitted_gp = distributions.GaussianProcess(
            train_ds['index_points'],
            learned_mean_fn,
            learned_kernel_fn, 1e-4
        )
        posterior_gp = fitted_gp.posterior_gp(
                train_ds['y'],
                xx_new,
                obs_noise_scale**2)

        pred_f_mean = posterior_gp.mean_function(xx_new)
        pred_f_var = jnp.diag(posterior_gp.kernel_function(xx_new, xx_new))

        fig, ax = plt.subplots()
        ax.fill_between(xx_new[:, 0],
                        pred_f_mean - 2*jnp.sqrt(pred_f_var),
                        pred_f_mean + 2*jnp.sqrt(pred_f_var), alpha=0.5)
        ax.plot(xx_new, posterior_gp.mean_function(xx_new), '-')
        ax.plot(train_ds['index_points'], train_ds['y'], 'ks')

        plt.show()


if __name__ == '__main__':
    app.run(main)
