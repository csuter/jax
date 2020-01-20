# coding=utf-8
# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
JAX user-facing transformations and utilities.

The transformations here mostly wrap internal transformations, providing
convenience flags to control behavior and handling Python containers of
arguments and outputs. The Python containers handled are pytrees (see
tree_util.py), which include nested tuples/lists/dicts, where the leaves are
arrays.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import functools
import itertools as it
import operator as op
import os
import threading
from warnings import warn

import numpy as onp
from contextlib import contextmanager
from distutils.util import strtobool

from . import core
from . import linear_util as lu
from . import ad_util
from . import dtypes
from .core import eval_jaxpr
from .api_util import (wraps, flatten_fun, apply_flat_fun, flatten_fun_nokwargs,
                       flatten_fun_nokwargs2)
from .tree_util import (tree_map, tree_flatten, tree_unflatten, tree_structure,
                        tree_transpose, tree_leaves, tree_multimap,
                        _replace_nones)
from .util import (unzip2, unzip3, curry, partial, safe_map, safe_zip,
                   WrapHashably, Hashable, prod, split_list, split_dict)
from .lib import xla_bridge as xb
from .lib.xla_bridge import (device_count, local_device_count, devices, local_devices,
                             host_id, host_ids, host_count)
from .abstract_arrays import ConcreteArray, ShapedArray, raise_to_shaped
from .interpreters import partial_eval as pe
from .interpreters import xla
from .interpreters import pxla
from .interpreters import ad
from .interpreters import batching
from .interpreters import parallel
from .interpreters import masking
from .interpreters.masking import shapecheck, ensure_poly
from .config import flags, config

map = safe_map
zip = safe_zip

FLAGS = flags.FLAGS
flags.DEFINE_bool("jax_disable_jit",
                  strtobool(os.getenv("JAX_DISABLE_JIT", "False")),
                  "Disable JIT compilation and just call original Python.")


def _check_callable(fun):
  if not callable(fun):
    raise TypeError("Expected a callable value, got {}".format(fun))

class _ThreadLocalState(threading.local):
  def __init__(self):
    self.jit_is_disabled = False

_thread_local_state = _ThreadLocalState()

def jit(fun, static_argnums=(), device=None, backend=None):
  """Sets up `fun` for just-in-time compilation with XLA.

  Args:
    fun: Function to be jitted. Should be a pure function, as side-effects may
      only be executed once. Its arguments and return value should be arrays,
      scalars, or (nested) standard Python containers (tuple/list/dict) thereof.

      Positional arguments indicated by `static_argnums` can be anything at all,
      provided they are hashable and have an equality operation defined. Static
      arguments are included as part of a compilation cache key, which is why
      hash and equality operators must be defined.
    static_argnums: A tuple of ints specifying which positional arguments to
      treat as static (compile-time constant). Operations that only depend on
      static arguments will be constant-folded. Calling the jitted function with
      different values for these constants will trigger recompilation. If the
      jitted function is called with fewer positional arguments than indicated
      by `static_argnums` then an error is raised. Defaults to ().
    device: This is an experimental feature and the API is likely to change.
      Optional, the Device the jitted function will run on. (Available devices
      can be retrieved via ``jax.devices()``.) The default is inherited from
      XLA's DeviceAssignment logic and is usually to use ``jax.devices()[0]``.
    backend: This is an experimental feature and the API is likely to change.
      Optional, a string representing the xla backend. 'cpu','gpu', or 'tpu'.

  Returns:
    A wrapped version of `fun`, set up for just-in-time compilation.

  In the following example, `selu` can be compiled into a single fused kernel by
  XLA:

  >>> @jax.jit
  >>> def selu(x, alpha=1.67, lmbda=1.05):
  >>>   return lmbda * jax.numpy.where(x > 0, x, alpha * jax.numpy.exp(x) - alpha)
  >>>
  >>> key = jax.random.PRNGKey(0)
  >>> x = jax.random.normal(key, (10,))
  >>> print(selu(x))
  [-0.54485154  0.27744263 -0.29255125 -0.91421586 -0.62452525 -0.2474813
   -0.8574326  -0.7823267   0.7682731   0.59566754]
  """
  _check_callable(fun)
  if isinstance(static_argnums, int):
    static_argnums = (static_argnums,)

  @wraps(fun)
  def f_jitted(*args, **kwargs):
    if _thread_local_state.jit_is_disabled or config.read('jax_disable_jit'):
      return fun(*args, **kwargs)
    if static_argnums and max(static_argnums) >= len(args):
      msg = ("Jitted function has static_argnums={} but was called with only {}"
             " positional arguments.")
      raise TypeError(msg.format(static_argnums, len(args)))
    f = lu.wrap_init(fun)
    if static_argnums:
      dyn_argnums = [i for i in range(len(args)) if i not in static_argnums]
      f, dyn_args = _argnums_partial(f, dyn_argnums, args)
    else:
      dyn_args = args
    args_flat, in_tree = tree_flatten((dyn_args, kwargs))
    _check_args(args_flat)
    flat_fun, out_tree = flatten_fun(f, in_tree)
    out = xla.xla_call(flat_fun, *args_flat, device=device, backend=backend)
    return tree_unflatten(out_tree(), out)

  jitted_name =  "jit({}, static_argnums={})"
  f_jitted.__name__ = jitted_name.format(f_jitted.__name__, static_argnums)
  return f_jitted

@contextmanager
def disable_jit():
  """Context manager that disables `jit` behavior under its dynamic context.

  For debugging purposes, it is useful to have a mechanism that disables `jit`
  everywhere in a dynamic context.

  Values that have a data dependence on the arguments to a jitted function are
  traced and abstracted. For example, an abstract value may be a ShapedArray
  instance, representing the set of all possible arrays with a given shape and
  dtype, but not representing one concrete array with specific values. You might
  notice those if you use a benign side-effecting operation in a jitted
  function, like a print:

  >>> @jax.jit
  >>> def f(x):
  ...   y = x * 2
  ...   print("Value of y is", y)
  ...   return y + 3
  ...
  >>> print(f(jax.numpy.array([1, 2, 3])))
  Value of y is Traced<ShapedArray(int32[3]):JaxprTrace(level=-1/1)>
  [5 7 9]

  Here `y` has been abstracted by `jit` to a `ShapedArray`, which represents an
  array with a fixed shape and type but an arbitrary value. It's also traced. If
  we want to see a concrete value while debugging, and avoid the tracer too, we
  can use the `disable_jit` context manager:

  >>> with jax.disable_jit():
  >>>   print(f(np.array([1, 2, 3])))
  >>>
  Value of y is [2 4 6]
  [5 7 9]
  """
  try:
    prev_val = _thread_local_state.jit_is_disabled
    _thread_local_state.jit_is_disabled = True
    yield
  finally:
    _thread_local_state.jit_is_disabled = prev_val


def xla_computation(fun, static_argnums=(), axis_env=None, backend=None,
                    tuple_args=False, instantiate_const_outputs=True):
  """Creates a function that produces its XLA computation given example args.

  Args:
    fun: Function from which to form XLA computations.
    static_argnums: See the ``jax.jit`` docstring.
    axis_env: Optional, a list of pairs where the first element is an axis name
      and the second element is a positive integer representing the size of the
      mapped axis with that name. This parameter is useful when lowering
      functions that involve parallel communication collectives, and it
      specifies the axis name/size environment that would be set up by
      applications of ``jax.pmap``. See the examples below.
    backend: This is an experimental feature and the API is likely to change.
      Optional, a string representing the xla backend. 'cpu','gpu', or 'tpu'.
    tuple_args: Optional bool, defaults to False. If True, the resulting XLA
      computation will have a single tuple argument that is unpacked into the
      specified function arguments.
    instantiate_const_outputs: Optional bool, defaults to True. If False, then
      ``xla_computation`` does not instantiate constant-valued outputs in the
      XLA computation, and so the result is closer to the computation that
      ``jax.jit`` produces and may be more useful for studying ``jit`` behavior.
      If True, then constant-valued outputs are instantiated in the XLA
      computation, which may be more useful for staging computations out of JAX
      entirely.

  Returns:
    A wrapped version of ``fun`` that when applied to example arguments returns a
    built XLA Computation (see xla_client.py), from which representations of the
    unoptimized XLA HLO computation can be extracted using methods like
    ``GetHloText``, ``GetSerializedProto``, and ``GetHloDotGraph``.

  For example:

  >>> def f(x): return jax.numpy.sin(jax.numpy.cos(x))
  >>> c = jax.xla_computation(f)(3.)
  >>> print(c.GetHloText())
  HloModule jaxpr_computation__4.5
  ENTRY jaxpr_computation__4.5 {
    tuple.1 = () tuple()
    parameter.2 = f32[] parameter(0)
    cosine.3 = f32[] cosine(parameter.2)
    ROOT sine.4 = f32[] sine(cosine.3)
  }

  Here's an example that involves a parallel collective and axis name:

  >>> def f(x): return x - jax.lax.psum(x, 'i')
  >>> c = jax.xla_computation(f, axis_env=[('i', 4)])(2)
  >>> print(c.GetHloText())
  HloModule jaxpr_computation.9
  primitive_computation.3 {
    parameter.4 = s32[] parameter(0)
    parameter.5 = s32[] parameter(1)
    ROOT add.6 = s32[] add(parameter.4, parameter.5)
  }
  ENTRY jaxpr_computation.9 {
    tuple.1 = () tuple()
    parameter.2 = s32[] parameter(0)
    all-reduce.7 = s32[] all-reduce(parameter.2), replica_groups={{0,1,2,3}}, to_apply=primitive_computation.3
    ROOT subtract.8 = s32[] subtract(parameter.2, all-reduce.7)
  }

  Notice the ``replica_groups`` that were generated. Here's an example that
  generates more interesting ``replica_groups``:

  >>> def g(x):
  ...   rowsum = lax.psum(x, 'i')
  ...   colsum = lax.psum(x, 'j')
  ...   allsum = lax.psum(x, ('i', 'j'))
  ...   return rowsum, colsum, allsum
  ...
  >>> axis_env = [('i', 4), ('j', 2)]
  >>> c = xla_computation(g, axis_env=axis_env)(5.)
  >>> print(c.GetHloText())
  HloModule jaxpr_computation__1.19
  [removed uninteresting text here]
  ENTRY jaxpr_computation__1.19 {
    tuple.1 = () tuple()
    parameter.2 = f32[] parameter(0)
    all-reduce.7 = f32[] all-reduce(parameter.2), replica_groups={{0,2,4,6},{1,3,5,7}}, to_apply=primitive_computation__1.3
    all-reduce.12 = f32[] all-reduce(parameter.2), replica_groups={{0,1},{2,3},{4,5},{6,7}}, to_apply=primitive_computation__1.8
    all-reduce.17 = f32[] all-reduce(parameter.2), replica_groups={{0,1,2,3,4,5,6,7}}, to_apply=primitive_computation__1.13
    ROOT tuple.18 = (f32[], f32[], f32[]) tuple(all-reduce.7, all-reduce.12, all-reduce.17)
  }
  """
  _check_callable(fun)
  fun_name = getattr(fun, '__name__', 'unknown')

  def make_axis_env(nreps):
    if axis_env is None:
      return xla.AxisEnv(nreps)
    else:
      nreps = nreps * prod(size for name, size in axis_env)
      names, sizes = zip(*axis_env)
      return xla.AxisEnv(nreps, names, sizes)

  @wraps(fun)
  def computation_maker(*args, **kwargs):
    wrapped = lu.wrap_init(fun)
    jax_args, in_tree = tree_flatten((args, kwargs))
    jaxtree_fun, out_tree = flatten_fun(wrapped, in_tree)
    avals = map(xla.abstractify, jax_args)
    pvals = [pe.PartialVal((aval, core.unit)) for aval in avals]
    jaxpr, _, consts = pe.trace_to_jaxpr(jaxtree_fun, pvals,
                                         instantiate=instantiate_const_outputs)
    axis_env_ = make_axis_env(xla.jaxpr_replicas(jaxpr))
    c = xb.make_computation_builder('xla_computation_{}'.format(fun_name))
    xla_consts = map(c.Constant, consts)
    xla_args = xla._xla_callable_args(c, avals, tuple_args)
    outs = xla.jaxpr_subcomp(c, jaxpr, backend, axis_env_, xla_consts, (),
                             *xla_args)
    return c.Build(c.Tuple(*outs))
  return computation_maker

def grad(fun, argnums=0, has_aux=False, holomorphic=False):
  """Creates a function which evaluates the gradient of `fun`.

  Args:
    fun: Function to be differentiated. Its arguments at positions specified by
      `argnums` should be arrays, scalars, or standard Python containers. It
      should return a scalar (which includes arrays with shape `()` but not
      arrays with shape `(1,)` etc.)
    argnums: Optional, integer or tuple of integers. Specifies which positional
      argument(s) to differentiate with respect to (default 0).
    has_aux: Optional, bool. Indicates whether `fun` returns a pair where the
      first element is considered the output of the mathematical function to be
      differentiated and the second element is auxiliary data. Default False.
    holomorphic: Optional, bool. Indicates whether `fun` is promised to be
      holomorphic. Default False.

  Returns:
    A function with the same arguments as `fun`, that evaluates the gradient of
    `fun`. If `argnums` is an integer then the gradient has the same shape and
    type as the positional argument indicated by that integer. If argnums is a
    tuple of integers, the gradient is a tuple of values with the same shapes
    and types as the corresponding arguments. If `has_aux` is True then a pair
    of (gradient, auxiliary_data) is returned.

  For example:

  >>> grad_tanh = jax.grad(jax.numpy.tanh)
  >>> print(grad_tanh(0.2))
  0.961043
  """
  value_and_grad_f = value_and_grad(fun, argnums, has_aux=has_aux,
                                    holomorphic=holomorphic)

  docstr = ("Gradient of {fun} with respect to positional argument(s) "
            "{argnums}. Takes the same arguments as {fun} but returns the "
            "gradient, which has the same shape as the arguments at "
            "positions {argnums}.")

  @wraps(fun, docstr=docstr, argnums=argnums)
  def grad_f(*args, **kwargs):
    _, g = value_and_grad_f(*args, **kwargs)
    return g

  @wraps(fun, docstr=docstr, argnums=argnums)
  def grad_f_aux(*args, **kwargs):
    (_, aux), g = value_and_grad_f(*args, **kwargs)
    return g, aux

  return grad_f_aux if has_aux else grad_f

def value_and_grad(fun, argnums=0, has_aux=False, holomorphic=False):
  """Creates a function which evaluates both `fun` and the gradient of `fun`.

  Args:
    fun: Function to be differentiated. Its arguments at positions specified by
      `argnums` should be arrays, scalars, or standard Python containers. It
      should return a scalar (which includes arrays with shape `()` but not
      arrays with shape `(1,)` etc.)
    argnums: Optional, integer or tuple of integers. Specifies which positional
      argument(s) to differentiate with respect to (default 0).
    has_aux: Optional, bool. Indicates whether `fun` returns a pair where the
     first element is considered the output of the mathematical function to be
     differentiated and the second element is auxiliary data. Default False.
    holomorphic: Optional, bool. Indicates whether `fun` is promised to be
      holomorphic. Default False.

  Returns:
    A function with the same arguments as `fun` that evaluates both `fun` and
    the gradient of `fun` and returns them as a pair (a two-element tuple). If
    `argnums` is an integer then the gradient has the same shape and type as the
    positional argument indicated by that integer. If argnums is a tuple of
    integers, the gradient is a tuple of values with the same shapes and types
    as the corresponding arguments.
  """

  docstr = ("Value and gradient of {fun} with respect to positional "
            "argument(s) {argnums}. Takes the same arguments as {fun} but "
            "returns a two-element tuple where the first element is the value "
            "of {fun} and the second element is the gradient, which has the "
            "same shape as the arguments at positions {argnums}.")

  _check_callable(fun)

  @wraps(fun, docstr=docstr, argnums=argnums)
  def value_and_grad_f(*args, **kwargs):
    max_argnum = argnums if type(argnums) is int else max(argnums)
    if max_argnum >= len(args):
      msg = ("differentiating with respect to argnums={} requires at least "
             "{} positional arguments to be passed by the caller, but got only "
             "{} positional arguments.")
      raise TypeError(msg.format(argnums, max_argnum + 1, len(args)))

    f = lu.wrap_init(fun, kwargs)
    f_partial, dyn_args = _argnums_partial(f, argnums, args)
    if not has_aux:
      ans, vjp_py = vjp(f_partial, *dyn_args)
    else:
      ans, vjp_py, aux = vjp(f_partial, *dyn_args, has_aux=True)
    _check_scalar(ans)
    dtype = dtypes.result_type(ans)
    if not (holomorphic or dtypes.issubdtype(dtype, onp.floating)):
      msg = ("Gradient only defined for real-output functions (with dtype that "
             "is a subdtype of np.floating), but got dtype {}. For holomorphic "
             "differentiation, pass holomorphic=True.")
      raise TypeError(msg.format(dtype))
    g = vjp_py(onp.ones((), dtype=dtype))
    g = g[0] if isinstance(argnums, int) else g
    if not has_aux:
      return ans, g
    else:
      return (ans, aux), g

  return value_and_grad_f

def _check_scalar(x):
  msg = "Gradient only defined for scalar-output functions. Output {}.".format
  try:
    aval = core.get_aval(x)
  except TypeError:
    raise TypeError(msg("was {}".format(x)))
  else:
    if isinstance(aval, ShapedArray):
      if aval.shape != ():
        raise TypeError(msg("had shape: {}".format(aval.shape)))
    else:
      raise TypeError(msg("had abstract value {}".format(aval)))


def jacfwd(fun, argnums=0, holomorphic=False):
  """Jacobian of `fun` evaluated column-by-column using forward-mode AD.

  Args:
    fun: Function whose Jacobian is to be computed.
    argnums: Optional, integer or tuple of integers. Specifies which positional
      argument(s) to differentiate with respect to (default `0`).
    holomorphic: Optional, bool. Indicates whether `fun` is promised to be
      holomorphic. Default False.

  Returns:
    A function with the same arguments as `fun`, that evaluates the Jacobian of
    `fun` using forward-mode automatic differentiation.

  >>> def f(x):
  ...   return jax.numpy.asarray(
  ...     [x[0], 5*x[2], 4*x[1]**2 - 2*x[2], x[2] * jax.numpy.sin(x[0])])
  ...
  >>> print(jax.jacfwd(f)(np.array([1., 2., 3.])))
  [[ 1.        ,  0.        ,  0.        ],
   [ 0.        ,  0.        ,  5.        ],
   [ 0.        , 16.        , -2.        ],
   [ 1.6209068 ,  0.        ,  0.84147096]]
  """

  def jacfun(*args, **kwargs):
    f = lu.wrap_init(fun, kwargs)
    f_partial, dyn_args = _argnums_partial(f, argnums, args)
    holomorphic or tree_map(_check_real_input_jacfwd, dyn_args)
    pushfwd = partial(jvp, f_partial, dyn_args)
    y, jac = vmap(pushfwd, out_axes=(None, batching.last))(_std_basis(dyn_args))
    example_args = dyn_args[0] if isinstance(argnums, int) else dyn_args
    return tree_map(partial(_unravel_array_into_pytree, example_args, -1), jac)

  return jacfun

def _check_real_input_jacfwd(x):
  aval = core.get_aval(x)
  if not dtypes.issubdtype(aval.dtype, onp.floating):
    msg = ("jacfwd only defined for functions with input dtypes that are "
           "sub-dtypes of `np.floating` (i.e. that model real values), but got "
           "{}. For holomorphic differentiation, pass holomorphic=True.")
    raise TypeError(msg.format(aval.dtype.name))


def jacrev(fun, argnums=0, holomorphic=False):
  """Jacobian of `fun` evaluated row-by-row using reverse-mode AD.

  Args:
    fun: Function whose Jacobian is to be computed.
    argnums: Optional, integer or tuple of integers. Specifies which positional
      argument(s) to differentiate with respect to (default `0`).
    holomorphic: Optional, bool. Indicates whether `fun` is promised to be
      holomorphic. Default False.

  Returns:
    A function with the same arguments as `fun`, that evaluates the Jacobian of
    `fun` using reverse-mode automatic differentiation.

  >>> def f(x):
  ...   return jax.numpy.asarray(
  ...     [x[0], 5*x[2], 4*x[1]**2 - 2*x[2], x[2] * jax.numpy.sin(x[0])])
  ...
  >>> print(jax.jacrev(f)(np.array([1., 2., 3.])))
  [[ 1.        ,  0.        ,  0.        ],
   [ 0.        ,  0.        ,  5.        ],
   [ 0.        , 16.        , -2.        ],
   [ 1.6209068 ,  0.        ,  0.84147096]]
  """
  def jacfun(*args, **kwargs):
    f = lu.wrap_init(fun, kwargs)
    f_partial, dyn_args = _argnums_partial(f, argnums, args)
    y, pullback = vjp(f_partial, *dyn_args)
    holomorphic or tree_map(_check_real_output_jacrev, y)
    jac = vmap(pullback)(_std_basis(y))
    jac = jac[0] if isinstance(argnums, int) else jac
    example_args = dyn_args[0] if isinstance(argnums, int) else dyn_args
    jac = tree_map(partial(_unravel_array_into_pytree, y, 0), jac)
    return tree_transpose(tree_structure(example_args), tree_structure(y), jac)

  return jacfun
jacobian = jacrev

def _check_real_output_jacrev(x):
  aval = core.get_aval(x)
  if not dtypes.issubdtype(aval.dtype, onp.floating):
    msg = ("jacrev only defined for functions with output dtypes that are "
           "sub-dtypes of `np.floating` (i.e. that model real values), but got "
           "{}. For holomorphic differentiation, pass holomorphic=True.")
    raise TypeError(msg.format(aval.dtype.name))


def hessian(fun, argnums=0, holomorphic=False):
  """Hessian of `fun`.

  Args:
    fun: Function whose Hessian is to be computed.
    argnums: Optional, integer or tuple of integers. Specifies which positional
      argument(s) to differentiate with respect to (default `0`).
    holomorphic: Optional, bool. Indicates whether `fun` is promised to be
      holomorphic. Default False.

  Returns:
    A function with the same arguments as `fun`, that evaluates the Hessian of
    `fun`.

  >>> g = lambda(x): x[0]**3 - 2*x[0]*x[1] - x[1]**6
  >>> print(jax.hessian(g)(jax.numpy.array([1., 2.])))
  [[   6.,   -2.],
   [  -2., -480.]]
  """
  return jacfwd(jacrev(fun, argnums, holomorphic), argnums, holomorphic)

def _std_basis(pytree):
  leaves, _ = tree_flatten(pytree)
  ndim = sum(map(onp.size, leaves))
  # TODO(mattjj): use a symbolic identity matrix here
  dtype = dtypes.result_type(*leaves)
  flat_basis = onp.eye(ndim, dtype=dtype)
  return _unravel_array_into_pytree(pytree, 1, flat_basis)

def _unravel_array_into_pytree(pytree, axis, arr):
  leaves, treedef = tree_flatten(pytree)
  axis = axis % arr.ndim
  shapes = [arr.shape[:axis] + onp.shape(l) + arr.shape[axis+1:] for l in leaves]
  parts = _split(arr, onp.cumsum(map(onp.size, leaves[:-1])), axis)
  reshaped_parts = [onp.reshape(x, shape) for x, shape in zip(parts, shapes)]
  return tree_unflatten(treedef, reshaped_parts)

def _split(x, indices, axis):
  if isinstance(x, onp.ndarray):
    return onp.split(x, indices, axis)
  else:
    return x.split(indices, axis)

def _dtype(x):
  return dtypes.canonicalize_dtype(dtypes.result_type(x))


def vmap(fun, in_axes=0, out_axes=0):
  """Vectorizing map. Creates a function which maps `fun` over argument axes.

  Args:
    fun: Function to be mapped over additional axes.
    in_axes: A nonnegative integer, None, or (nested) standard Python container
      (tuple/list/dict) thereof specifying which input array axes to map over.
      If each positional argument to ``fun`` is an array, then ``in_axes`` can
      be a nonnegative integer, a None, or a tuple of integers and Nones with
      length equal to the number of positional arguments to ``fun``. An integer
      or None indicates which array axis to map over for all arguments (with
      None indicating not to map any axis), and a tuple indicates which axis to
      map for each corresponding positional argument. If the positional
      arguments to ``fun`` are container types, the corresponding element of
      ``in_axes`` can itself be a matching container, so that distinct array
      axes can be mapped for different container elements. ``in_axes`` must be a
      container tree prefix of the positional argument tuple passed to ``fun``.
    out_axes: A nonnegative integer, None, or (nested) standard Python container
      (tuple/list/dict) thereof indicating where the mapped axis should appear
      in the output.

  Returns:
    Batched/vectorized version of ``fun`` with arguments that correspond to
    those of ``fun``, but with extra array axes at positions indicated by
    ``in_axes``, and a return value that corresponds to that of ``fun``, but
    with extra array axes at positions indicated by ``out_axes``.

  For example, we can implement a matrix-matrix product using a vector dot
  product:

  >>> vv = lambda x, y: np.vdot(x, y)  #  ([a], [a]) -> []
  >>> mv = vmap(vv, (0, None), 0)      #  ([b,a], [a]) -> [b]      (b is the mapped axis)
  >>> mm = vmap(mv, (None, 1), 1)      #  ([b,a], [a,c]) -> [b,c]  (c is the mapped axis)

  Here we use ``[a,b]`` to indicate an array with shape (a,b). Here are some
  variants:

  >>> mv1 = vmap(vv, (0, 0), 0)   #  ([b,a], [b,a]) -> [b]        (b is the mapped axis)
  >>> mv2 = vmap(vv, (0, 1), 0)   #  ([b,a], [a,b]) -> [b]        (b is the mapped axis)
  >>> mm2 = vmap(mv2, (1, 1), 0)  #  ([b,c,a], [a,c,b]) -> [c,b]  (c is the mapped axis)

  Here's an example of using container types in ``in_axes`` to specify which
  axes of the container elements to map over:

  >>> A, B, C, D = 2, 3, 4, 5
  >>> x = np.ones((A, B))
  >>> y = np.ones((B, C))
  >>> z = np.ones((C, D))
  >>> def foo(tree_arg):
  ...   x, (y, z) = tree_arg
  ...   return np.dot(x, np.dot(y, z))
  >>> tree = (x, (y, z))
  >>> print(foo(tree))
  [[12. 12. 12. 12. 12.]
   [12. 12. 12. 12. 12.]]
  >>> from jax import vmap
  >>> K = 6  # batch size
  >>> x = np.ones((K, A, B))  # batch axis in different locations
  >>> y = np.ones((B, K, C))
  >>> z = np.ones((C, D, K))
  >>> tree = (x, (y, z))
  >>> vfoo = vmap(foo, in_axes=((0, (1, 2)),))
  >>> print(vfoo(tree)).shape
  (6, 2, 5)
  """
  docstr = ("Vectorized version of {fun}. Takes similar arguments as {fun} "
            "but with additional array axes over which {fun} is mapped.")

  _check_callable(fun)
  if (not isinstance(in_axes, (list, tuple, type(None), int))
      or not isinstance(out_axes, (list, tuple, type(None), int))):
    msg = ("vmap arguments in_axes and out_axes must each be an integer, None, "
           "or a (nested) tuple of those types, got {} and {} respectively.")
    raise TypeError(msg.format(type(in_axes), type(out_axes)))

  def _check_axis_sizes(tree, vals, dims):
    mapped_axis_sizes = {x.shape[d] for x, d in zip(vals, dims) if d is not None}
    try:
      sizes, = mapped_axis_sizes
    except ValueError:
      msg = "vmap got inconsistent sizes for array axes to be mapped:\n{}"
      # we switch the error message based on whether args is a tuple of arrays,
      # in which case we can produce an error message based on argument indices,
      # or if it has nested containers.
      # TODO(mattjj,phawkins): add a way to inspect pytree kind more directly
      if tree == tree_flatten((core.unit,) * tree.num_leaves)[1]:
        lines1 = ["arg {} has shape {} and axis {} is to be mapped"
                  .format(i, x.shape, d) for i, (x, d) in enumerate(zip(vals, dims))]
        sizes = collections.defaultdict(list)
        for i, (x, d) in enumerate(zip(vals, dims)):
          if d is not None:
            sizes[x.shape[d]].append(i)
        lines2 = ["{} {} {} {} to be mapped of size {}".format(
                   "args" if len(idxs) > 1 else "arg",
                   ", ".join(map(str, idxs)),
                   "have" if len(idxs) > 1 else "has",
                   "axes" if len(idxs) > 1 else "an axis",
                   size)
                  for size, idxs in sizes.items()]
        raise ValueError(msg.format("\n".join(lines1 + ["so"] + lines2)))
      else:
        sizes = [x.shape[d] if d is not None else None for x, d in zip(vals, dims)]
        sizes = tree_unflatten(tree, sizes)
        raise ValueError(msg.format("the tree of axis sizes is:\n{}".format(sizes)))

  @wraps(fun, docstr=docstr)
  def batched_fun(*args):
    args_flat, in_tree  = tree_flatten(args)
    f = lu.wrap_init(fun)
    flat_fun, out_tree = flatten_fun_nokwargs(f, in_tree)
    in_axes_flat = _flatten_axes(in_tree, in_axes)
    _check_axis_sizes(in_tree, args_flat, in_axes_flat)
    out_flat = batching.batch(flat_fun, args_flat, in_axes_flat,
                              lambda: _flatten_axes(out_tree(), out_axes))
    return tree_unflatten(out_tree(), out_flat)

  return batched_fun

def _flatten_axes(treedef, axis_tree):
  # given an axis spec tree axis_tree (a pytree with integers and Nones at the
  # leaves, i.e. the Nones are to be considered leaves) that is a tree prefix of
  # the given treedef, build a complete axis spec tree with the same structure
  # and return the flattened result
  # TODO(mattjj,phawkins): improve this implementation
  proxy = object()
  dummy = tree_unflatten(treedef, [object()] * treedef.num_leaves)
  axes = []
  add_leaves = lambda i, x: axes.extend([i] * len(tree_flatten(x)[0]))
  try:
    tree_multimap(add_leaves, _replace_nones(proxy, axis_tree), dummy)
  except ValueError:
    msg = ("axes specification must be a tree prefix of the corresponding "
           "value, got specification {} for value {}.")
    raise ValueError(msg.format(axis_tree, treedef))
  axes = [None if a is proxy else a for a in axes]
  assert len(axes) == treedef.num_leaves
  return axes


def pmap(fun, axis_name=None, devices=None, backend=None, axis_size=None):
  """Parallel map with support for collectives.

  The purpose of ``pmap`` is to express single-program multiple-data (SPMD)
  programs. Applying ``pmap`` to a function will compile the function with XLA
  (similarly to ``jit``), then execute it in parallel on XLA devices, such as
  multiple GPUs or multiple TPU cores. Semantically it is comparable to
  ``vmap`` because both transformations map a function over array axes, but
  where ``vmap`` vectorizes functions by pushing the mapped axis down into
  primitive operations, ``pmap`` instead replicates the function and executes
  each replica on its own XLA device in parallel.

  Another key difference with ``vmap`` is that while ``vmap`` can only express
  pure maps, ``pmap`` enables the use of parallel SPMD collective operations,
  like all-reduce sum.

  The mapped axis size must be less than or equal to the number of local XLA
  devices available, as returned by ``jax.local_device_count()`` (unless
  ``devices`` is specified, see below). For nested ``pmap`` calls, the product
  of the mapped axis sizes must be less than or equal to the number of XLA
  devices.

  **Multi-host platforms:** On multi-host platforms such as TPU pods, ``pmap``
  is designed to be used in SPMD Python programs, where every host is running
  the same Python code such that all hosts run the same pmapped function in the
  same order. Each host should still call the pmapped function with mapped axis
  size equal to the number of *local* devices (unless ``devices`` is specified,
  see below), and an array of the same leading axis size will be returned as
  usual. However, any collective operations in ``fun`` will be computed over
  *all* participating devices, including those on other hosts, via
  device-to-device communication.  Conceptually, this can be thought of as
  running a pmap over a single array sharded across hosts, where each host
  "sees" only its local shard of the input and output. The SPMD model requires
  that the same multi-host pmaps must be run in the same order on all devices,
  but they can be interspersed with arbitrary operations running on a single
  host.

  Args:
    fun: Function to be mapped over argument axes. Its arguments and return
      value should be arrays, scalars, or (nested) standard Python containers
      (tuple/list/dict) thereof.
    axis_name: Optional, a hashable Python object used to identify the mapped
      axis so that parallel collectives can be applied.
    devices: This is an experimental feature and the API is likely to change.
      Optional, a sequence of Devices to map over. (Available devices can be
      retrieved via jax.devices()). If specified, the size of the mapped axis
      must be equal to the number of local devices in the sequence. Nested
      ``pmap`` s with ``devices`` specified in either the inner or outer ``pmap``
      are not yet supported.
    backend: This is an experimental feature and the API is likely to change.
      Optional, a string representing the xla backend. 'cpu', 'gpu', or 'tpu'.

  Returns:
    A parallelized version of ``fun`` with arguments that correspond to those of
    ``fun`` but each with an additional leading array axis (with equal sizes)
    and with output that has an additional leading array axis (with the same
    size).

  For example, assuming 8 XLA devices are available, ``pmap`` can be used as a
  map along a leading array axes:

  >>> out = pmap(lambda x: x ** 2)(np.arange(8))
  >>> print(out)
  [0, 1, 4, 9, 16, 25, 36, 49]
  >>> x = np.arange(3 * 2 * 2.).reshape((3, 2, 2))
  >>> y = np.arange(3 * 2 * 2.).reshape((3, 2, 2)) ** 2
  >>> out = pmap(np.dot)(x, y)
  >>> print(out)
  [[[    4.     9.]
    [   12.    29.]]
   [[  244.   345.]
    [  348.   493.]]
   [[ 1412.  1737.]
    [ 1740.  2141.]]]

  In addition to expressing pure maps, ``pmap`` can also be used to express
  parallel single-program multiple-data (SPMD) programs that communicate via
  collective operations. For example:

  >>> f = lambda x: x / jax.lax.psum(x, axis_name='i')
  >>> out = pmap(f, axis_name='i')(np.arange(4.))
  >>> print(out)
  [ 0.          0.16666667  0.33333334  0.5       ]
  >>> print(out.sum())
  1.0

  In this example, ``axis_name`` is a string, but it can be any Python object
  with ``__hash__`` and ``__eq__`` defined.

  The argument ``axis_name`` to ``pmap`` names the mapped axis so that
  collective operations, like ``jax.lax.psum``, can refer to it. Axis names are
  important particularly in the case of nested ``pmap`` functions, where
  collectives can operate over distinct axes:

  >>> from functools import partial
  >>> @partial(pmap, axis_name='rows')
  >>> @partial(pmap, axis_name='cols')
  >>> def normalize(x):
  >>>   row_normed = x / jax.lax.psum(x, 'rows')
  >>>   col_normed = x / jax.lax.psum(x, 'cols')
  >>>   doubly_normed = x / jax.lax.psum(x, ('rows', 'cols'))
  >>>   return row_normed, col_normed, doubly_normed
  >>>
  >>> x = np.arange(8.).reshape((4, 2))
  >>> row_normed, col_normed, doubly_normed = normalize(x)
  >>> print(row_normed.sum(0))
  [ 1.  1.]
  >>> print(col_normed.sum(1))
  [ 1.  1.  1.  1.]
  >>> print(doubly_normed.sum((0, 1)))
  1.0

  On multi-host platforms, collective operations operate over all devices,
  including those those on other hosts. For example, assuming the following code
  runs on two hosts with 4 XLA devices each:

  >>> f = lambda x: x + jax.lax.psum(x, axis_name='i')
  >>> data = np.arange(4) if jax.host_id() == 0 else np.arange(4,8)
  >>> out = pmap(f, axis_name='i')(data)
  >>> print(out)
  [28 29 30 31] # on host 0
  [32 33 34 35] # on host 1

  Each host passes in a different length-4 array, corresponding to its 4 local
  devices, and the psum operates over all 8 values. Conceptually, the two
  length-4 arrays can be thought of as sharded length-16 array (in this example
  equivalent to np.arange(8)) that is mapped over, with the length-8 mapped axis
  given name 'i'. The pmap call on each host then returns the corresponding
  length-4 output shard.

  The ``devices`` argument can be used to specify exactly which devices are used
  to run the parallel computation. For example, again assuming a single host
  with 8 devices, the following code defines two parallel computations, one
  which runs on the first six devices and one on the remaining two:

  >>> from functools import partial
  >>> @partial(pmap, axis_name='i', devices=jax.devices()[:6])
  >>> def f1(x):
  >>>   return x / jax.lax.psum(x, axis_name='i')
  >>>
  >>> @partial(pmap, axis_name='i', devices=jax.devices()[-2:])
  >>> def f2(x):
  >>>   return jax.lax.psum(x ** 2, axis_name='i')
  >>>
  >>> print(f1(np.arange(6.)))
  [0.         0.06666667 0.13333333 0.2        0.26666667 0.33333333]
  >>> print(f2(np.array([2., 3.])))
  [ 13.  13.]
  """
  _check_callable(fun)
  axis_name = _TempAxisName(fun) if axis_name is None else axis_name

  # axis_size is an optional integer representing the global axis size.
  # The aggregate size (across all hosts) size of the mapped axis must match
  # the given value. This argument is mutually exclusive with ``devices``.
  if axis_size is not None and devices is not None:
    msg = "pmap got devices and axis_size. They're mutually exclusive."
    raise ValueError(msg)

  @wraps(fun)
  def f_pmapped(*args, **kwargs):
    f = lu.wrap_init(fun)
    args, in_tree = tree_flatten((args, kwargs))
    local_axis_size = _pmap_axis_size(args)
    _check_args(args)
    flat_fun, out_tree = flatten_fun(f, in_tree)
    out = pxla.xla_pmap(
        flat_fun,
        *args,
        axis_name=axis_name,
        axis_size=local_axis_size,
        global_axis_size=axis_size,
        devices=tuple(devices) if devices is not None else devices,
        backend=backend)
    return tree_unflatten(out_tree(), out)

  namestr = "pmap({}, axis_name={})".format
  f_pmapped.__name__ = namestr(f_pmapped.__name__, axis_name)
  return f_pmapped

def _pmap_axis_size(xs):
  for x in xs:
    try:
      return x.shape[0]
    except AttributeError:
      pass
  else:
    msg = "pmap got value with no leading axis to map over: {}."
    raise ValueError(msg.format([x for x in xs if not hasattr(x, 'shape')]))

class _TempAxisName(object):
  def __init__(self, obj):
    self.obj = obj
  def __repr__(self):
    return '<axis {}>'.format(hex(id(self.obj)))
  def __hash__(self):
    return hash(self.obj)
  def __eq__(self, other):
    return self.obj is other.obj


def soft_pmap(fun, axis_name=None, backend=None):
  warn("soft_pmap is an experimental feature and probably has bugs!")
  _check_callable(fun)
  axis_name = _TempAxisName(fun) if axis_name is None else axis_name

  @wraps(fun)
  def f_pmapped(*args, **kwargs):
    f = lu.wrap_init(fun)
    args_flat, in_tree = tree_flatten((args, kwargs))
    axis_size = _pmap_axis_size(args_flat)
    _check_args(args_flat)
    flat_fun, out_tree = flatten_fun(f, in_tree)

    chunk_size, leftover = divmod(axis_size, pxla.unmapped_device_count(backend))
    if chunk_size == 0 and leftover:
      return pmap(fun, axis_name, backend)(*args)  # can map directly onto hardware
    elif leftover:
      msg = ("soft_pmap mapped axis size must be divisble by the number of "
             "XLA devices (or be less than or equal to that number), but got "
             "an axis size of {} with {} devices.")
      raise ValueError(msg.format(axis_size, pxla.unmapped_device_count()))
    num_chunks = axis_size // chunk_size

    reshaped_args = [_reshape_split(num_chunks, x) for x in args_flat]
    soft_mapped_fun = pxla.split_axis(flat_fun, axis_name, chunk_size)
    reshaped_outs = pxla.xla_pmap(soft_mapped_fun, *reshaped_args,
                                  axis_name=axis_name, axis_size=num_chunks,
                                  global_axis_size=None, devices=None,
                                  backend=backend)
    outs = [_reshape_merge(out) for out in reshaped_outs]
    return tree_unflatten(out_tree(), outs)

  namestr = "soft_pmap({}, axis_name={})".format
  f_pmapped.__name__ = namestr(f_pmapped.__name__, axis_name)
  return f_pmapped

def _reshape_split(num_chunks, x):
  aval = core.get_aval(x)
  if aval is core.abstract_unit:
    return x
  else:
    return x.reshape((num_chunks, x.shape[0] // num_chunks) + x.shape[1:])

def _reshape_merge(x):
  aval = core.get_aval(x)
  if aval is core.abstract_unit:
    return x
  else:
    return x.reshape((-1,) + x.shape[2:])


def _papply(fun):
  # This function is for testing purposes.
  axis_name = _TempAxisName(fun)

  def papply_fun(*args, **kwargs):
    f = lu.wrap_init(fun)
    args_flat, in_tree = tree_flatten((args, kwargs))
    flat_fun, out_tree = flatten_fun(f, in_tree)
    axis_size = _pmap_axis_size(args_flat)
    out_flat = parallel.papply(flat_fun, axis_name, args_flat, axis_size)
    return tree_unflatten(out_tree(), out_flat)

  return papply_fun, axis_name


def _parallelize(fun):
  axis_name = _TempAxisName(fun)

  def pfun(*args):
    f = lu.wrap_init(fun)
    args_flat, in_tree = tree_flatten(args)
    f, out_tree = flatten_fun_nokwargs(f, in_tree)
    axis_size = _pmap_axis_size(args_flat)

    chunk_size, leftover = divmod(axis_size, pxla.unmapped_device_count())
    if chunk_size == 0 and leftover:
      return pmap(fun, axis_name)(*args)  # can map directly onto hardware
    elif leftover:
      raise ValueError
    num_chunks = axis_size // chunk_size

    reshaped_args = [_reshape_split(num_chunks, x) for x in args_flat]
    f, out_axes = parallel.papply_transform(f, axis_name, axis_size)
    f = pxla.split_axis(f, axis_name, chunk_size)
    outs = pxla.xla_pmap(f, *reshaped_args, axis_name=axis_name,
                         axis_size=num_chunks, global_axis_size=None,
                         devices=None, backend=None)
    outs = map(_reshape_merge, outs)
    outs = [batching.matchaxis(axis_size, 0, dst, x)
            for dst, x in zip(out_axes(), outs)]
    return tree_unflatten(out_tree(), outs)

  return pfun


def mask(fun, in_shapes, out_shape):
  in_specs, in_shapes_tree = tree_flatten(in_shapes)
  out_specs, out_shapes_tree = tree_flatten(out_shape)

  in_specs = map(masking.parse_spec, in_specs)
  out_specs = map(masking.parse_spec, out_specs)

  unique_ids = collections.defaultdict(object)
  in_specs  = map(partial(_remap_ids, unique_ids), in_specs)
  out_specs = map(partial(_remap_ids, unique_ids), out_specs)

  def wrapped_fun(args, logical_env):
    args_flat, in_tree = tree_flatten(args)
    if in_tree != in_shapes_tree: raise TypeError("pytree mismatch")
    logical_env = {unique_ids[name] : val for name, val in logical_env.items()}
    in_shapes = map(masking.finalize_spec, in_specs, map(onp.shape, args_flat))
    padded_env = _bind_shapes(in_shapes, [x.shape for x in args_flat])
    f = lu.wrap_init(fun)
    flat_fun, out_tree = flatten_fun_nokwargs(f, in_tree)
    outs, out_shapes_ = masking.mask_fun(
        flat_fun, logical_env, padded_env, args_flat, in_shapes)
    if not out_tree() == out_shapes_tree: raise TypeError("pytree mismatch")
    out_shapes = map(masking.finalize_spec, out_specs, map(onp.shape, outs))
    if not out_shapes == list(out_shapes_):
      raise masking.ShapeError
    if not all(onp.shape(out) == masking.eval_shape_expr(padded_env, expr)
               for out, expr in zip(outs, out_shapes)):
      raise masking.ShapeError
    return tree_unflatten(out_tree(), outs)
  return wrapped_fun

def _remap_ids(names, shape_spec):
  ShapeSpec, Poly, Mon = masking.ShapeSpec, masking.Poly, masking.Mon
  mdim = masking.monomorphic_dim
  return ShapeSpec(Poly({Mon({names[id] : deg for id, deg in mon.items()})
                          : coeff for mon, coeff in poly.items()})
                   if poly is not mdim else mdim for poly in shape_spec)

def _bind_shapes(shape_exprs, shapes):
  env = {}
  for shape_expr, shape in zip(shape_exprs, shapes):
    for poly, d in zip(shape_expr, shape):
      if ensure_poly(poly).is_constant:
        continue
      else:
        (binder,), = poly  # TODO generalize to handle striding
        if env.setdefault(binder, d) != d: raise masking.ShapeError
  return env


@curry
def shapecheck(in_shapes, out_shape, fun):
  in_shapes, in_tree = tree_flatten(in_shapes)
  in_shapes = map(masking.parse_spec, in_shapes)
  out_shapes, out_tree = tree_flatten(out_shape)
  out_shapes = map(masking.parse_spec, out_shapes)
  flat_fun, out_tree_ = flatten_fun_nokwargs(lu.wrap_init(fun), in_tree)
  out_shapes_ = masking.shapecheck(flat_fun, in_shapes)
  if out_tree != out_tree_(): raise TypeError("pytree mismatch")
  if not all(map(_shape_spec_consistent, out_shapes, out_shapes_)):
    raise masking.ShapeError
  return fun

def _shape_spec_consistent(spec, expr):
  return all(a == b for a, b in zip(spec, expr) if a is not masking.monomorphic_dim)


def jvp(fun, primals, tangents):
  """Computes a (forward-mode) Jacobian-vector product of `fun`.

  Args:
    fun: Function to be differentiated. Its arguments should be arrays, scalars,
      or standard Python containers of arrays or scalars. It should return an
      array, scalar, or standard Python container of arrays or scalars.
    primals: The primal values at which the Jacobian of `fun` should be
      evaluated. Should be either a tuple or a list of arguments,
      and its length should  equal to the number of positional parameters of `fun`.
    tangents: The tangent vector for which the Jacobian-vector product should be
      evaluated. Should be either a tuple or a list of tangents, with the same
      tree structure and array shapes as `primals`.

  Returns:
    A `(primals_out, tangents_out)` pair, where `primals_out` is
    `fun(*primals)`, and `tangents_out` is the Jacobian-vector product of
    `function` evaluated at `primals` with `tangents`. The `tangents_out` value
    has the same Python tree structure and shapes as `primals_out`.

  For example:

  >>> y, v = jax.jvp(jax.numpy.sin, (0.1,), (0.2,))
  >>> print(y)
  0.09983342
  >>> print(v)
  0.19900084
  """
  if not isinstance(fun, lu.WrappedFun):
    fun = lu.wrap_init(fun)

  if (not isinstance(primals, (tuple, list)) or
      not isinstance(tangents, (tuple, list))):
    msg = ("primal and tangent arguments to jax.jvp must be tuples or lists; "
           "found {} and {}.")
    raise TypeError(msg.format(type(primals).__name__, type(tangents).__name__))

  ps_flat, tree_def = tree_flatten(primals)
  ts_flat, tree_def_2 = tree_flatten(tangents)
  if tree_def != tree_def_2:
    msg = ("primal and tangent arguments to jax.jvp must have the same tree "
           "structure; primals have tree structure {} whereas tangents have "
           "tree structure {}")
    raise TypeError(msg.format(tree_def, tree_def_2))
  for p, t in safe_zip(ps_flat, ts_flat):
    if _dtype(p) != _dtype(t):
      msg = ("primal and tangent arguments to jax.jvp must have equal types; "
             "type mismatch primal {} vs tangent {}")
      raise TypeError(msg.format(_dtype(p), _dtype(t)))
  flat_fun, out_tree = flatten_fun_nokwargs(fun, tree_def)
  out_primals, out_tangents = ad.jvp(flat_fun).call_wrapped(ps_flat, ts_flat)
  return (tree_unflatten(out_tree(), out_primals),
          tree_unflatten(out_tree(), out_tangents))

def linearize(fun, *primals):
  """Produce a linear approximation to `fun` using `jvp` and partial evaluation.

  Args:
    fun: Function to be differentiated. Its arguments should be arrays, scalars,
      or standard Python containers of arrays or scalars. It should return an
      array, scalar, or standard python container of arrays or scalars.
    primals: The primal values at which the Jacobian of `fun` should be
      evaluated. Should be a tuple of arrays, scalar, or standard Python
      container thereof. The length of the tuple is equal to the number of
      positional parameters of `fun`.

  Returns:
    A pair where the first element is the value of `f(*primals)` and the second
    element is a function that evaluates the (forward-mode) Jacobian-vector
    product of `fun` evaluated at `primals` without re-doing the linearization
    work.

  In terms of values computed, `linearize` behaves much like a curried `jvp`,
  where these two code blocks compute the same values::
    y, out_tangent = jax.jvp(f, (x,), (in_tangent,))

    y, f_jvp = jax.linearize(f, x)
    out_tangent = f_jvp(in_tangent)

  However, the difference is that `linearize` uses partial evaluation so that
  the function `f` is not re-linearized on calls to `f_jvp`. In general that
  means the memory usage scales with the size of the computation, much like in
  reverse-mode. (Indeed, `linearize` has a similar signature to `vjp`!)

  This function is mainly useful if you want to apply `f_jvp` multiple times,
  i.e. to evaluate a pushforward for many different input tangent vectors at the
  same linearization point. Moreover if all the input tangent vectors are known
  at once, it can be more efficient to vectorize using `vmap`, as in::
    pushfwd = partial(jvp, f, (x,))
    y, out_tangents = vmap(pushfwd, out_axes=(None, 0))((in_tangents,))
  By using `vmap` and `jvp` together like this we avoid the stored-linearization
  memory cost that scales with the depth of the computation, which is incurred
  by both `linearize` and `vjp`.

  Here's a more complete example of using `linearize`:

  >>> def f(x): return 3. * np.sin(x) + np.cos(x / 2.)
  ...
  >>> jax.jvp(f, (2.,), (3.,))
  (array(3.2681944, dtype=float32), array(-5.007528, dtype=float32))
  >>> y, f_jvp = jax.linearize(f, 2.)
  >>> print(y)
  3.2681944
  >>> print(f_jvp(3.))
  -5.007528
  >>> print(f_jvp(4.))
  -6.676704
  """
  f = lu.wrap_init(fun)
  primals_flat, in_tree = tree_flatten((primals, {}))
  jaxtree_fun, out_tree = flatten_fun(f, in_tree)
  out_primals, out_pvals, jaxpr, consts = ad.linearize(jaxtree_fun, *primals_flat)
  out_tree = out_tree()
  out_primal_py = tree_unflatten(out_tree, out_primals)
  primal_avals = list(map(core.get_aval, primals_flat))
  lifted_jvp = partial(lift_linearized, jaxpr, primal_avals, consts,
                       (in_tree, out_tree), out_pvals)
  return out_primal_py, lifted_jvp

def lift_linearized(jaxpr, primal_avals, consts, io_tree, out_pvals, *py_args):
  def fun(*tangents):
    tangent_avals = list(map(core.get_aval, tangents))
    for primal_aval, tangent_aval in zip(primal_avals, tangent_avals):
      try:
        core.lattice_join(primal_aval, tangent_aval)
      except TypeError:
        msg = ("linearized function called on tangent values inconsistent with "
               "the original primal values.")
        raise ValueError(msg)
    dummy = (core.unit,) * len(tangents)
    out = eval_jaxpr(jaxpr, consts, (), *(dummy + tangents))
    tangents_out = out[len(out)//2:]
    return tuple(map(pe.merge_pvals, tangents_out, out_pvals))

  return apply_flat_fun(fun, io_tree, *py_args)

def _check_inexact_input_vjp(x):
  aval = core.get_aval(x)
  if not dtypes.issubdtype(aval.dtype, onp.inexact):
    msg = ("Primal inputs to reverse-mode differentiation must be of float "
           "or complex type, got type {}")
    raise TypeError(msg.format(aval.dtype.name))

def _vjp_pullback_wrapper(fun, cotangent_dtypes, io_tree, py_args):
  in_tree_expected, out_tree = io_tree
  args, in_tree = tree_flatten(py_args)
  if in_tree != in_tree_expected:
    msg = ("Tree structure of cotangent input {}, does not match structure of "
           "primal output {}")
    raise TypeError(msg.format(in_tree_expected, in_tree))
  for a, dtype in safe_zip(args, cotangent_dtypes):
    if _dtype(a) != dtype:
      msg = ("Type of cotangent input to vjp pullback function ({}) does not "
             "match type of corresponding primal output ({})")
      raise TypeError(msg.format(_dtype(a), dtype))
  ans = fun(*args)
  return tree_unflatten(out_tree, ans)


def vjp(fun, *primals, **kwargs):
  """Compute a (reverse-mode) vector-Jacobian product of `fun`.

  `grad` is implemented as a special case of `vjp`.

  Args:
    fun: Function to be differentiated. Its arguments should be arrays, scalars,
      or standard Python containers of arrays or scalars. It should return an
      array, scalar, or standard Python container of arrays or scalars.
    primals: A sequence of primal values at which the Jacobian of `fun`
      should be evaluated. The length of `primals` should be equal to the number
      of positional parameters to `fun`. Each primal value should be a tuple of
      arrays, scalar, or standard Python containers thereof.
    has_aux: Optional, bool. Indicates whether `fun` returns a pair where the
     first element is considered the output of the mathematical function to be
     differentiated and the second element is auxiliary data. Default False.

  Returns:
    A `(primals_out, vjpfun)` pair, where `primals_out` is `fun(*primals)`.
    `vjpfun` is a function from a cotangent vector with the same shape as
    `primals_out` to a tuple of cotangent vectors with the same shape as
    `primals`, representing the vector-Jacobian product of `fun` evaluated at
    `primals`.

  >>> def f(x, y):
  ...   return jax.numpy.sin(x), jax.numpy.cos(y)
  ...
  >>> primals, f_vjp = jax.vjp(f, 0.5, 1.0)
  >>> xbar, ybar = f_vjp((-0.7, 0.3))
  >>> print(xbar)
  -0.61430776
  >>> print(ybar)
  -0.2524413
  """
  has_aux = kwargs.pop('has_aux', False)
  assert not kwargs
  if not isinstance(fun, lu.WrappedFun):
    fun = lu.wrap_init(fun)
  primals_flat, in_tree = tree_flatten(primals)
  _check_args(primals_flat)
  tree_map(_check_inexact_input_vjp, primals)
  if not has_aux:
    flat_fun, out_tree = flatten_fun_nokwargs(fun, in_tree)
    out_primal, out_vjp = ad.vjp(flat_fun, primals_flat)
    out_tree = out_tree()
  else:
    flat_fun, out_aux_trees = flatten_fun_nokwargs2(fun, in_tree)
    out_primal, out_vjp, aux = ad.vjp(flat_fun, primals_flat, has_aux=True)
    out_tree, aux_tree = out_aux_trees()
  out_primal_py = tree_unflatten(out_tree, out_primal)
  vjp_py = partial(_vjp_pullback_wrapper, out_vjp,
                   [_dtype(x) for x in out_primal], (out_tree, in_tree))
  if not has_aux:
    return out_primal_py, vjp_py
  else:
    return out_primal_py, vjp_py, tree_unflatten(aux_tree, aux)


def make_jaxpr(fun):
  """Creates a function that produces its jaxpr given example args.

  Args:
    fun: The function whose `jaxpr` is to be computed. Its positional arguments
      and return value should be arrays, scalars, or standard Python containers
      (tuple/list/dict) thereof.

  Returns:
    A wrapped version of `fun` that when applied to example arguments returns a
    TypedJaxpr representation of `fun` on those arguments.

  A `jaxpr` is JAX's intermediate representation for program traces. The `jaxpr`
  language is based on the simply-typed first-order lambda calculus with
  let-bindings. `make_jaxpr` adapts a function to return its `jaxpr`, which we
  can inspect to understand what JAX is doing internally.

  The `jaxpr` returned is a trace of `fun` abstracted to `ShapedArray` level.
  Other levels of abstraction exist internally.

  We do not describe the semantics of the `jaxpr` language in detail here, but
  instead give a few examples.

  >>> def f(x): return jax.numpy.sin(jax.numpy.cos(x))
  >>> print(f(3.0))
  -0.83602184
  >>> jax.make_jaxpr(f)(3.0)
  { lambda  ;  ; a.
    let b = cos a
        c = sin b
    in [c] }
  >>> jax.make_jaxpr(jax.grad(f))(3.0)
  { lambda  ;  ; a.
    let b = cos a
        c = cos b
        d = mul 1.0 c
        e = neg d
        f = sin a
        g = mul e f
    in [g] }
  """
  _check_callable(fun)

  def pv_like(x):
    aval = xla.abstractify(x)
    return pe.PartialVal((aval, core.unit))

  @wraps(fun)
  def jaxpr_maker(*args, **kwargs):
    wrapped = lu.wrap_init(fun)
    jax_args, in_tree = tree_flatten((args, kwargs))
    jaxtree_fun, out_tree = flatten_fun(wrapped, in_tree)
    in_pvals = map(pv_like, jax_args)
    jaxpr, out_pvals, consts = pe.trace_to_jaxpr(
        jaxtree_fun, in_pvals, instantiate=True, stage_out_calls=True)
    out_avals = map(raise_to_shaped, unzip2(out_pvals)[0])
    in_avals = tuple(raise_to_shaped(in_aval) for in_aval, _ in in_pvals)
    typed_jaxpr = core.TypedJaxpr(jaxpr, consts, in_avals, out_avals)
    return typed_jaxpr

  jaxpr_maker.__name__ = "make_jaxpr({})".format(jaxpr_maker.__name__)
  return jaxpr_maker


def device_put(x, device=None):
  return tree_map(lambda y: xla.device_put_p.bind(y, device=device), x)


# TODO(mattjj): consider revising
def _device_get(x):
  if isinstance(x, core.Tracer):
    return x
  return x.copy()

def device_get(x):
  for y in tree_leaves(x):
    try:
      y.copy_to_host_async()
    except AttributeError:
      pass
  return tree_map(_device_get, x)


def _argnums_partial(f, dyn_argnums, args):
  if isinstance(dyn_argnums, int):
    dyn_argnums = (dyn_argnums,)
  else:
    dyn_argnums = tuple(dyn_argnums)
  fixed_args = tuple([core.unit if i in dyn_argnums else _wrap_hashably(arg)
                      for i, arg in enumerate(args)])
  dyn_args = tuple(args[i] for i in dyn_argnums)
  return _argnums_partial_(f, dyn_argnums, fixed_args), dyn_args

def _wrap_hashably(arg):
  try:
    hash(arg)
  except TypeError:
    return WrapHashably(arg)  # e.g. ndarrays, DeviceArrays
  else:
    return Hashable(arg)

@lu.transformation
def _argnums_partial_(dyn_argnums, fixed_args, *dyn_args, **kwargs):
  args = [None if arg is core.unit else arg.val for arg in fixed_args]
  for i, arg in zip(dyn_argnums, dyn_args):
    args[i] = arg
  ans = yield args, kwargs
  yield ans

def _check_args(args):
  for arg in args:
    if not (isinstance(arg, core.Tracer) or _valid_jaxtype(arg)):
      raise TypeError("Argument '{}' of type {} is not a valid JAX type"
                      .format(arg, type(arg)))

def _valid_jaxtype(arg):
  try:
    xla.abstractify(arg)  # faster than core.get_aval
  except TypeError:
    return False
  else:
    return True


class ShapeDtypeStruct(object):
  __slots__ = ["shape", "dtype"]
  def __init__(self, shape, dtype):
    self.shape = shape
    self.dtype = dtype

def eval_shape(fun, *args, **kwargs):
  """Compute the shape/dtype of ``fun(*args, **kwargs)`` without any FLOPs.

  This utility function is useful for performing shape inference. Its
  input/output behavior is defined by::

    def eval_shape(fun, *args, **kwargs):
      out = fun(*args, **kwargs)
      return jax.tree_util.tree_map(shape_dtype_struct, out)

    def shape_dtype_struct(x):
      return ShapeDtypeStruct(x.shape, x.dtype)

    class ShapeDtypeStruct(object):
      __slots__ = ["shape", "dtype"]
      def __init__(self, shape, dtype):
        self.shape = shape
        self.dtype = dtype

  In particular, the output is a pytree of objects that have ``shape`` and
  ``dtype`` attributes, but nothing else about them is guaranteed by the API.

  But instead of applying ``fun`` directly, which might be expensive, it uses
  JAX's abstract interpretation machinery to evaluate the shapes without doing
  any FLOPs.

  Using ``eval_shape`` can also catch shape errors, and will raise same shape
  errors as evaluating ``fun(*args, **kwargs)``.

  Args:
    *args: a positional argument tuple of arrays, scalars, or (nested) standard
      Python containers (tuples, lists, dicts, namedtuples, i.e. pytrees) of
      those types. Since only the ``shape`` and ``dtype`` attributes are
      accessed, only values that duck-type arrays are required, rather than real
      ndarrays. The duck-typed objects cannot be namedtuples because those are
      treated as standard Python containers. See the example below.
    **kwargs: a keyword argument dict of arrays, scalars, or (nested) standard
      Python containers (pytrees) of those types. As in ``args``, array values
      need only be duck-typed to have ``shape`` and ``dtype`` attributes.

  For example:

  >>> f = lambda A, x: np.tanh(np.dot(A, x))
  >>> class MyArgArray(object):
  ...   def __init__(self, shape, dtype):
  ...     self.shape = shape
  ...     self.dtype = dtype
  ...
  >>> A = MyArgArray((2000, 3000), np.float32)
  >>> x = MyArgArray((3000, 1000), np.float32)
  >>> out = jax.eval_shape(f, A, x)  # no FLOPs performed
  >>> print(out.shape)
  (2000, 1000)
  >>> print(out.dtype)
  dtype('float32')
  """
  def abstractify(x):
    return ShapedArray(onp.shape(x), dtypes.result_type(x))
  args_flat, in_tree = tree_flatten((args, kwargs))
  fun, out_tree = flatten_fun(lu.wrap_init(fun), in_tree)
  out = pe.abstract_eval_fun(fun.call_wrapped, *map(abstractify, args_flat))
  out = [ShapeDtypeStruct(x.shape, x.dtype) for x in out]
  return tree_unflatten(out_tree(), out)


def checkpoint(fun, concrete=False):
  @wraps(fun)
  def fun_remat(*args, **kwargs):
    args_flat, in_tree = tree_flatten((args, kwargs))
    flat_fun, out_tree = flatten_fun(lu.wrap_init(fun), in_tree)
    out_flat = pe.remat_call(flat_fun, *args_flat, concrete=concrete)
    return tree_unflatten(out_tree(), out_flat)
  return fun_remat
remat = checkpoint


def custom_jvp(fwd, jvp):
  @wraps(fwd)
  def fun_(*args, **kwargs):
    args_flat, in_tree = tree_flatten((args, kwargs))
    flat_fun, out_data = _flatten_fun_and_count_res(lu.wrap_init(fwd), in_tree)
    out_flat = custom_jvp_call(flat_fun, *args_flat, out_data=out_data, jvp=jvp,
                               in_tree=in_tree, keep_res=False)
    ans_tree, _, _ = out_data()
    return tree_unflatten(ans_tree, out_flat)
  return fun_

def custom_vjp(fwd, bwd):
  @wraps(fwd)
  def fun_(*args, **kwargs):
    args_flat, in_tree = tree_flatten((args, kwargs))
    flat_fun, out_data = _flatten_fun_and_count_res(lu.wrap_init(fwd), in_tree)
    out_flat = custom_vjp_call(flat_fun, *args_flat, bwd=bwd, out_data=out_data,
                               keep_res=False)
    ans_tree, _, _ = out_data()
    return tree_unflatten(ans_tree, out_flat)
  return fun_

def _custom_call_bind(primitive, f, *args, **params):
  top_trace = core.find_top_trace(args)
  level = (core.trace_state.trace_stack.next_level(True)
           if top_trace is None else top_trace.level)
  f = _check_for_env_traces(f, level)
  if top_trace is None:
    with core.new_sublevel():
      return primitive.impl(f, *args, **params)
  else:
    tracers = map(top_trace.full_raise, args)
    outs = top_trace.process_custom_call(primitive, f, tracers, params)
    outs = map(core.full_lower, outs)
  return outs

def _custom_call_impl(f, *args, **params):
  keep_res = params.pop('keep_res')
  bdims, stores = params.pop('bdims', ()), params.pop('stores', ())
  out_bdims = []
  for bd_in in bdims:
    f, bd_out = batching.batch_fun2(f, True, bd_in)
    out_bdims.append(bd_out)
  outs = f.call_wrapped(*args, keep_res=keep_res)
  for bd_out, s in zip(out_bdims, stores):
    s.store(bd_out())
  return outs

custom_vjp_call_p = core.Primitive('custom_vjp_call')
custom_vjp_call = partial(_custom_call_bind, custom_vjp_call_p)
custom_vjp_call_p.def_custom_bind(custom_vjp_call)
custom_vjp_call_p.def_impl(_custom_call_impl)

custom_jvp_call_p = core.Primitive('custom_jvp_call')
custom_jvp_call = partial(_custom_call_bind, custom_jvp_call_p)
custom_jvp_call_p.def_custom_bind(custom_jvp_call)
custom_jvp_call_p.def_impl(_custom_call_impl)

@lu.transformation_with_aux
def _flatten_fun_and_count_res(in_tree_, *args_flat, **params):
  py_args, py_kwargs = tree_unflatten(in_tree_, args_flat)
  ans, res = yield py_args, py_kwargs
  ans_flat, ans_tree = tree_flatten(ans)
  if params['keep_res']:
    res = res() if callable(res) else res
    res_flat, res_tree = tree_flatten(res)
    num_res = len(res_flat)
    yield res_flat + ans_flat, (ans_tree, res_tree, num_res)
  else:
    yield ans_flat, (ans_tree, None, 0)

@lu.transformation
def _check_for_env_traces(level, *args, **kwargs):
  outs = yield args, kwargs
  if any(isinstance(x, core.Tracer) and x.trace.level > level for x in outs):
    raise Exception("closed-over traced values")
  yield outs

# This transpose rule doesn't live in ad.py because it needs batching.py
def _custom_linearized_transpose(cts_in, *invals, **params):
  nres, argnums, bwd, avals_out, ans_tree, res_tree, bdims_in, bdims_res, bdims_out = \
      split_dict(params, ['num_res', 'argnums', 'bwd', 'avals_out', 'ans_tree',
                          'res_tree', 'bdims_in', 'bdims_res', 'bdims_out'])
  res, in_primals = split_list(invals, [nres])
  assert all(x is ad.undefined_primal for x in in_primals)
  del in_primals
  cts_in = [ad.instantiate_zeros_aval(a, ct) for a, ct in zip(avals_out, cts_in)]

  @lu.wrap_init
  def bwd_(*res_cts_flat):
    res, cts_in = split_list(res_cts_flat, [nres])
    cts_out = bwd(tuple(argnums), tree_unflatten(res_tree, res),
                  tree_unflatten(ans_tree, cts_in))
    cts_out_flat, _ = tree_flatten(cts_out)
    return cts_out_flat

  for bd_res, bd_out, bd_in in zip(bdims_res, bdims_out, bdims_in):
    bwd_ = batching.batch_fun(bwd_, bd_res + bd_out, lambda: bd_in)
  cts_out_flat = bwd_.call_wrapped(*(res + cts_in))
  assert len(cts_out_flat) == len(argnums), (cts_out_flat, argnums)

  return [None] * nres + cts_out_flat
ad.primitive_transposes[ad.custom_linearized_p] = _custom_linearized_transpose

# This partial eval rule doesn't lives here because it needs batching.py.
def _partial_eval_custom_call(trace, call_primitive, fun, tracers, params):
  bdims, stores = params.get('bdims', ()), params.get('stores', ())
  out_bdims = []
  for bd_in in bdims:
    fun, bd_out = batching.batch_fun2(fun, True, bd_in)
    out_bdims.append(bd_out)
  outs = fun.call_wrapped(*tracers, keep_res=params['keep_res'])
  for bd_out, s in zip(out_bdims, stores):
    s.store(bd_out())
  return outs
pe.JaxprTrace.process_custom_call = _partial_eval_custom_call
