# Copyright 2018 The TensorFlow Probability Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""The SchurComplement kernel."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
from tensorflow_probability.python.bijectors import cholesky_outer_product
from tensorflow_probability.python.bijectors import invert
from tensorflow_probability.python.internal import distribution_util
from tensorflow_probability.python.internal import dtype_util
from tensorflow_probability.python.positive_semidefinite_kernels import positive_semidefinite_kernel as psd_kernel
from tensorflow_probability.python.positive_semidefinite_kernels.internal import util


__all__ = [
    'SchurComplement',
]


def _validate_arg_if_not_none(arg, assertion, validate_args):
  if arg is None:
    return arg
  with tf.control_dependencies([assertion(arg)] if validate_args else []):
    result = tf.identity(arg)
  return result


def _add_diagonal_shift(matrix, shift):
  return tf.linalg.set_diag(
      matrix, tf.linalg.diag_part(matrix) + shift, name='add_diagonal_shift')


class SchurComplement(psd_kernel.PositiveSemidefiniteKernel):
  """The SchurComplement kernel.

  Given a block matrix `M = [[A, B], [C, D]]`, the Schur complement of D in M is
  written `M / D = A - B @ Inverse(D) @ C`.

  This class represents a PositiveSemidefiniteKernel whose behavior is as
  follows. We compute a matrix, analogous to `D` in the above definition, by
  calling `base_kernel.matrix(fixed_inputs, fixed_inputs)`. Then given new input
  locations `x` and `y`, we can construct the remaining pieces of `M` above, and
  compute the Schur complement of `D` in `M` (see Mathematical Details, below).

  Notably, this kernel uses a bijector (Invert(CholeskyOuterProduct)), as an
  intermediary for the requisite matrix solve, which means we get a caching
  benefit after the first use.

  ### Mathematical Details

  Suppose we have a kernel `k` and some fixed collection of inputs
  `Z = [z0, z1, ..., zN]`. Given new inputs `x` and `y`, we can form a block
  matrix

   ```none
     M = [
       [k(x, y), k(x, z0), ..., k(x, zN)],
       [k(z0, y), k(z0, z0), ..., k(z0, zN)],
       ...,
       [k(zN, y), k(z0, zN), ..., k(zN, zN)],
     ]
   ```

  We might write this, so as to emphasize the block structure,

   ```none
     M = [
       [xy, xZ],
       [yZ^T, ZZ],
     ],

     xy = [k(x, y)]
     xZ = [k(x, z0), ..., k(x, zN)]
     yZ = [k(y, z0), ..., k(y, zN)]
     ZZ = "the matrix of k(zi, zj)'s"
   ```

  Then we have the definition of this kernel's apply method:

  `schur_comp.apply(x, y) = xy - xZ @ ZZ^{-1} @ yZ^T`

  and similarly, if x and y are collections of inputs.

  As with other PSDKernels, the `apply` method acts as a (possibly
  vectorized) scalar function of 2 inputs. Given a single `x` and `y`,
  `apply` will yield a scalar output. Given two (equal size!) collections `X`
  and `Y`, it will yield another (equal size!) collection of scalar outputs.

  ### Examples

  Here's a simple example usage, with no particular motivation.

  ```python
  from tensorflow_probability import positive_semidefinite_kernels as psd_kernel

  base_kernel = psd_kernel.ExponentiatedQuadratic(amplitude=np.float64(1.))
  # 3 points in 1-dimensional space (shape [3, 1]).
  z = [[0.], [3.], [4.]]

  schur_kernel = psd_kernel.SchurComplement(
      base_kernel=base_kernel,
      fixed_inputs=z)

  # Two individual 1-d points
  x = [1.]
  y = [2.]
  print(schur_kernel.apply(x, y))
  # ==> k(x, y) - k(x, z) @ Inverse(k(z, z)) @ k(z, y)
  ```

  A more motivating application of this kernel is in constructing a Gaussian
  process that is conditioned on some observed data.

  ```python
  from tensorflow_probability import distributions as tfd
  from tensorflow_probability import positive_semidefinite_kernels as psd_kernel

  base_kernel = psd_kernel.ExponentiatedQuadratic(amplitude=np.float64(1.))
  observation_index_points = np.random.uniform(-1., 1., [50, 1])
  observations = np.sin(2 * np.pi * observation_index_points[..., 0])

  posterior_kernel = psd_kernel.SchurComplement(
      base_kernel=base_kernel,
      fixed_inputs=observation_index_points)

  # Assume we use a zero prior mean, and compute the posterior mean.
  def posterior_mean_fn(x):
    k_x_obs_linop = tf.linalg.LinearOperatorFullMatrix(
        base_kernel.matrix(x, observation_index_points))
    chol_linop = tf.linalg.LinearOperatorLowerTriangular(
        posterior_kernel.divisor_matrix_cholesky)

    return k_x_obs_linop.matvec(
        chol_linop.solvevec(
            chol_linop.solvevec(observations),
            adjoint=True))

  # Construct the GP posterior distribution at some new points.
  gp_posterior = tfp.distributions.GaussianProcess(
      index_points=np.linspace(-1., 1., 100)[..., np.newaxis],
      kernel=posterior_kernel,
      mean_fn=posterior_mean_fn)

  # Draw 5 samples on the above 100-point grid
  samples = gp_posterior.sample(5)
  ```

  """

  def __init__(self,
               base_kernel,
               fixed_inputs,
               diag_shift=None,
               validate_args=False,
               name='SchurComplement'):
    """Construct a SchurComplement kernel instance.

    Args:
      base_kernel: A `PositiveSemidefiniteKernel` instance, the kernel used to
        build the block matrices of which this kernel computes the  Schur
        complement.
      fixed_inputs: A Tensor, representing a collection of inputs. The Schur
        complement that this kernel computes comes from a block matrix, whose
        bottom-right corner is derived from `base_kernel.matrix(fixed_inputs,
        fixed_inputs)`, and whose top-right and bottom-left pieces are
        constructed by computing the base_kernel at pairs of input locations
        together with these `fixed_inputs`. `fixed_inputs` is allowed to be an
        empty collection (either `None` or having a zero shape entry), in which
        case the kernel falls back to the trivial application of `base_kernel`
        to inputs. See class-level docstring for more details on the exact
        computation this does; `fixed_inputs` correspond to the `Z` structure
        discussed there. `fixed_inputs` is assumed to have shape `[b1, ..., bB,
        N, f1, ..., fF]` where the `b`'s are batch shape entries, the `f`'s are
        feature_shape entries, and `N` is the number of fixed inputs. Use of
        this kernel entails a 1-time O(N^3) cost of computing the Cholesky
        decomposition of the k(Z, Z) matrix. The batch shape elements of
        `fixed_inputs` must be broadcast compatible with
        `base_kernel.batch_shape`.
      diag_shift: A floating point scalar to be added to the diagonal of the
        divisor_matrix before computing its Cholesky.
      validate_args: If `True`, parameters are checked for validity despite
        possibly degrading runtime performance.
        Default value: `False`
      name: Python `str` name prefixed to Ops created by this class.
        Default value: `"SchurComplement"`
    """
    with tf.compat.v1.name_scope(
        name, values=[base_kernel, fixed_inputs]) as name:
      # If the base_kernel doesn't have a specified dtype, we can't pass it off
      # to common_dtype, which always expects `tf.as_dtype(dtype)` to work (and
      # it doesn't if the given `dtype` is None.
      # TODO(b/130421035): Consider changing common_dtype to allow Nones, and
      # clean this up after.
      #
      # Thus, we spell out the logic
      # here: use the dtype of `fixed_inputs` if possible. If base_kernel.dtype
      # is not None, use the usual logic.
      if base_kernel.dtype is None:
        dtype = None if fixed_inputs is None else fixed_inputs.dtype
      else:
        dtype = dtype_util.common_dtype([base_kernel, fixed_inputs], tf.float32)
      self._base_kernel = base_kernel
      self._fixed_inputs = (None if fixed_inputs is None else
                            tf.convert_to_tensor(value=fixed_inputs,
                                                 dtype=dtype))
      if not self._is_fixed_inputs_empty():
        # We create and store this matrix here, so that we get the caching
        # benefit when we later access its cholesky. If we computed the matrix
        # every time we needed the cholesky, the bijector cache wouldn't be hit.
        self._divisor_matrix = base_kernel.matrix(fixed_inputs, fixed_inputs)
        if diag_shift is not None:
          broadcast_shape = distribution_util.get_broadcast_shape(
              self._divisor_matrix, diag_shift[..., tf.newaxis])
          self._divisor_matrix = tf.broadcast_to(
              self._divisor_matrix, broadcast_shape)
          self._divisor_matrix = _add_diagonal_shift(
              self._divisor_matrix, diag_shift)

      self._cholesky_bijector = invert.Invert(
          cholesky_outer_product.CholeskyOuterProduct())
    super(SchurComplement, self).__init__(
        base_kernel.feature_ndims, dtype=dtype, name=name)

  def _is_fixed_inputs_empty(self):
    # If fixed_inputs are `None` or have size 0, we consider this empty and fall
    # back to (cheaper) trivial behavior.
    if self._fixed_inputs is None:
      return True
    num_fixed_inputs = tf.compat.dimension_value(
        self._fixed_inputs.shape[-(self._base_kernel.feature_ndims + 1)])
    if num_fixed_inputs is not None and num_fixed_inputs == 0:
      return True
    return False

  def _batch_shape(self):
    if self._is_fixed_inputs_empty():
      return self._base_kernel.batch_shape
    return tf.broadcast_static_shape(
        self._base_kernel.batch_shape,
        self._fixed_inputs.shape[
            :-(self._base_kernel.feature_ndims + 1)])

  def _batch_shape_tensor(self):
    if self._is_fixed_inputs_empty():
      return self._base_kernel.batch_shape_tensor()
    return tf.broadcast_dynamic_shape(
        self._base_kernel.batch_shape_tensor(),
        tf.shape(input=self._fixed_inputs)[
            :-(self._base_kernel.feature_ndims + 1)])

  def _apply(self, x1, x2, example_ndims):
    # In the shape annotations below,
    #
    #  - x1 has shape B1 + E1 + F  (batch, example, feature),
    #  - x2 has shape B2 + E2 + F,
    #  - z refers to self.fixed_inputs, and has shape Bz + [ez] + F, ie its
    #    example ndims is exactly 1,
    #  - self.base_kernel has batch shape Bk,
    #  - bc(A, B, C) means "the result of broadcasting shapes A, B, and C".

    # Shape: bc(Bk, B1, B2) + bc(E1, E2)
    k12 = self.base_kernel.apply(x1, x2, example_ndims)
    if self._is_fixed_inputs_empty():
      return k12

    # Shape: bc(Bk, B1, Bz) + E1 + [ez]
    k1z = self.base_kernel.tensor(x1, self.fixed_inputs,
                                  x1_example_ndims=example_ndims,
                                  x2_example_ndims=1)

    # Shape: bc(Bk, B2, Bz) + E2 + [ez]
    k2z = self.base_kernel.tensor(x2, self.fixed_inputs,
                                  x1_example_ndims=example_ndims,
                                  x2_example_ndims=1)

    # Shape: bc(Bz, Bk) + [ez, ez]
    div_mat_chol = self._cholesky_bijector.forward(self._divisor_matrix)

    # Shape: bc(Bz, Bk) + [1, ..., 1] + [ez, ez]
    #                      `--------'
    #                          `-- (example_ndims - 1) ones
    # This reshape ensures that the batch shapes here align correctly with the
    # batch shape of k2z, below: `example_ndims` because E2 has rank
    # `example_ndims`, and "- 1" because one of the ez's here already "pushed"
    # the batch dims over by one.
    div_mat_chol = util.pad_shape_with_ones(div_mat_chol, example_ndims - 1, -3)
    div_mat_chol_linop = tf.linalg.LinearOperatorLowerTriangular(div_mat_chol)

    # Shape: bc(Bz, Bk, B2) + E2 + [ez]
    kzzinv_kz2 = tf.linalg.matrix_transpose(
        # Shape: bc(Bz, Bk, B2) + E2[:-1] + [ez] + E2[-1]
        div_mat_chol_linop.solve(
            # Shape: bc(Bz, Bk, B2) + E2[:-1] + [ez] + E2[-1]
            div_mat_chol_linop.solve(k2z, adjoint_arg=True),
            adjoint=True))

    # Shape: bc(Bz, Bk, B1, B2) + bc(E1, E2)
    k1z_kzzinv_kz2 = tf.reduce_sum(
        # Shape: bc(Bz, Bk, B1, B2) + bc(E1, E2) + [ez]
        input_tensor=k1z * kzzinv_kz2,
        axis=-1)  # we can safely always reduce just this one trailing dim,
                  # since self.fixed_inputs is presumed to have example_ndims
                  # exactly 1.

    # Shape: bc(Bz, Bk, B1, B2) + bc(E1, E2)
    return k12 - k1z_kzzinv_kz2

  @property
  def fixed_inputs(self):
    return self._fixed_inputs

  @property
  def base_kernel(self):
    return self._base_kernel

  @property
  def cholesky_bijector(self):
    return self._cholesky_bijector

  @property
  def divisor_matrix(self):
    return self._divisor_matrix

  @property
  def divisor_matrix_cholesky(self):
    return self.cholesky_bijector.forward(self.divisor_matrix)
