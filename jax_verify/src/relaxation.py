# coding=utf-8
# Copyright 2020 The jax_verify Authors.
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

"""Propagate the relaxation through the network.

This is accomplished by traversing the JaxPR representation of the computation
and translating the computational graph.
"""
import abc
import functools
from typing import Callable, Tuple, Dict, Union, List, Optional

import jax
from jax import lax
import jax.numpy as jnp
from jax_verify.src import bound_propagation
from jax_verify.src import ibp
import numpy as np

Tensor = jnp.ndarray


class RelaxVariable(bound_propagation.Bound):
  """Variable used to build relaxation."""

  def __init__(self, name, base_bound):
    self.shape = base_bound.lower.shape
    self.base_bound = base_bound
    self.name = name
    self.constraints = None

  def set_constraints(self, constraints):
    self.constraints = constraints

  @property
  def lower(self):
    return self.base_bound.lower

  @property
  def upper(self):
    return self.base_bound.upper


class LinearConstraint:
  """Linear constraint, to be encoded into a solver."""

  def __init__(self, vars_and_coeffs, bias, sense):
    self._vars_and_coeffs = vars_and_coeffs
    self._bias = bias
    self.sense = sense
    self.sample_dependent = bool(self._bias.shape)

  def bias(self, index: int):
    """Get the bias corresponding to the minibatch sample `index`.

    If bias has a dimension, it means it is sample dependent. Otherwise,
    the bias is the same for all samples.

    Args:
      index: Index in the batch for which to build the variable.
    Returns:
      bias: value of the bias.
    """
    return self._bias[index] if self.sample_dependent else self._bias

  def vars_and_coeffs(self, index: int):
    """Get the variable and coefficients corresponding to the sample `index`.

    If coeffs has a dimension, the coefficients are sample dependent. Otherwise,
    the coefficients are the same for all samples.

    Args:
      index: Index in the batch for which to build the variable.
    Returns:
      vars_and_coeffs: vars_and_coeffs list where the coefficients are the one
        corresponding to the sample `index`
    """
    if self.sample_dependent:
      return [(var, (cpts, coeffs[index]))
              for (var, (cpts, coeffs)) in self._vars_and_coeffs]
    else:
      return self._vars_and_coeffs

  def encode_into_solver(self, solver: 'RelaxationSolver', index: int):
    """Encode the linear constraints into the provided solver.

    Args:
      solver: RelaxationSolver to create the linear constraint into.
      index: Index in the batch for which to build the variable.
    """
    solver.create_linear_solver_constraint(self, index)


class RelaxActivationConstraint:
  """Linear constraint involved in the relaxation of an activation."""

  def __init__(self, outvar, invar, scale, bias, sense):
    """Represents the constraint outvar =(>)(<) scale * invar + bias."""
    self.outvar = outvar
    self.invar = invar
    self.scale = scale
    self.bias = bias
    self.sense = sense

  def encode_into_solver(self, solver: 'RelaxationSolver', index: int):
    """Encode the linear constraints into the provided solver.

    Args:
      solver: RelaxationSolver to create the linear constraint into.
      index: Index in the batch for which to build the variable.
    """
    biases = np.reshape(self.bias[index, ...], [-1])
    slopes = np.reshape(self.scale[index, ...], [-1])
    for act_index, (slope, bias) in enumerate(zip(slopes, biases)):
      solver.create_activation_solver_constraint(
          self, act_index, slope.item(), bias.item())


class RelaxationSolver(metaclass=abc.ABCMeta):
  """Abstract solver for the relaxation."""

  @abc.abstractmethod
  def create_solver_variable(self, relax_var: RelaxVariable, index: int):
    """Create a new bound-constrained solver variable based on a RelaxVariable.

    Args:
      relax_var: Variable generated by the relaxation bound propagation.
      index: Index in the batch for which to build the variable.
    """

  @abc.abstractmethod
  def create_linear_solver_constraint(self, constraint: LinearConstraint,
                                      index: int):
    """Create a new solver linear constraint.

    Args:
      constraint: Constraint generated by the relaxation bound propagation.
      index: Index in the batch for which to build the variable.
    """

  @abc.abstractmethod
  def create_activation_solver_constraint(
      self, constraint: RelaxActivationConstraint, act_index: int,
      slope: float, bias: float):
    """Create the linear constraint involved in the activation relaxation.

    Args:
      constraint: Constraint generated by the relaxation bound propagation.
      act_index: Index of the activation to encode (in the variables involved
        in constraint)
      slope : Slope coefficients of the linear inequality.
      bias: Bias of the linear inequality
    """

  @abc.abstractmethod
  def minimize_objective(self,
                         var_name: int,
                         objective: Tensor,
                         objective_bias: float,
                         time_limit: Optional[int]) -> Tuple[float, bool]:
    """Minimize a linear function.

    Args:
      var_name: Index of the variable to define a linear function over the
        components.
      objective: Coefficients of the linear function.
      objective_bias: Bias of the linear function.
      time_limit: Maximum solve time in ms. Use None for unbounded.
    Returns:
      val: Value of the minimum.
      status: Status of the optimization function.
    """


def solve_relaxation(solver_ctor: Callable[[], RelaxationSolver],
                     objective: Tensor,
                     objective_bias: float,
                     variable_opt: RelaxVariable,
                     env: Dict[int, Union[RelaxVariable, Tensor]],
                     index: int,
                     time_limit: Optional[int] = None) -> Tuple[float, bool]:
  """Solves the relaxation using the provided LP solver.

  Args:
    solver_ctor: Constructor for the solver.
    objective: Objective to optimize, given as an array of coefficients to be
      applied to the variable to form a linear objective function
    objective_bias: Bias to add to objective
    variable_opt: RelaxVariable over which the linear function to optimize
      is defined.
    env: Environment created by applying boundprop with relaxation.py
    index: The index in the minibatch for which the LP should be solved
    time_limit: Time limit on solver. None if unbounded.
  Returns:
    opt: Value of the solution found.
    status: Whether the optimal solution has been achieved.
  """
  solver = solver_ctor()
  for key in env.keys():
    if isinstance(env[key], RelaxVariable):
      variable = env[key]
      # Create the variable in the solver.
      solver.create_solver_variable(variable, index)
      # Create the constraints in the solver.
      if variable.constraints:
        for constraint in variable.constraints:
          constraint.encode_into_solver(solver, index)
  return solver.minimize_objective(variable_opt.name,
                                   objective, objective_bias, time_limit)


def _get_linear(primitive, outval, *eqn_invars, **params):
  """Get linear expressions corresponding to an affine layer.

  Args:
    primitive: jax primitive
    outval: dummy tensor shaped according to a single example's outputs
    *eqn_invars: Arguments of the primitive, wrapped as RelaxVariables
    **params: Keyword Arguments of the primitive.

  Returns:
    For each output component, a pair `(bias, coefficients)`, where
    `coefficients` is a list of `(component, coefficient)` pairs.
  """
  def funx(x):
    if isinstance(x, RelaxVariable):
      return jnp.zeros_like(jnp.expand_dims(x.lower[0, ...], 0))
    else:
      return x
  def fungrad(i, args):
    return jnp.reshape(primitive.bind(*args, **params)[0, ...], [-1])[i]
  results = []
  # Loop over output dimensions one at a time to avoid creating a large
  # materialized tensor
  # TODO: Replace with something more efficient
  for i in range(outval.size):
    fung = functools.partial(fungrad, i)
    bias, current_grad = jax.value_and_grad(fung)(
        [funx(x) for x in eqn_invars])
    coefficients = []
    for res in current_grad:
      components = jnp.flatnonzero(res)
      coefficients.append((components, res.ravel()[components]))
    results.append((bias, coefficients))
  return results


def _get_relu_relax(lower, upper):
  """Upper chord of triangle relu relaxation."""
  on = lower >= 0.
  amb = jnp.logical_and(lower < 0., upper > 0.)
  slope = jnp.where(on, jnp.ones_like(lower), jnp.zeros_like(lower))
  slope += jnp.where(amb, upper/jnp.maximum(upper-lower, 1e-12),
                     jnp.zeros_like(lower))
  bias = jnp.where(amb, -lower * upper/jnp.maximum(upper-lower, 1e-12),
                   jnp.zeros_like(lower))
  return slope, bias


def _relax_input(
    index: int, in_bounds: bound_propagation.Bound,
    ) -> RelaxVariable:
  """Generates the initial inputs for the relaxation.

  Args:
    index: Integer identifying the input node.
    in_bounds: Concrete bounds on the input node.
  Returns:
    `RelaxVariable` for the initial inputs.
  """
  # Wrap initial bound as RelaxVariable bound.
  in_variable = RelaxVariable(index, in_bounds)
  return in_variable


_affine_primitives_list = [lax.broadcast_in_dim_p, lax.add_p,
                           lax.conv_general_dilated_p,
                           lax.dot_general_p, lax.sub_p,
                           lax.mul_p]
_activation_list = [lax.max_p]
_passthrough_list = [lax.reshape_p]


def _relax_primitive(
    index: int, out_bounds: bound_propagation.Bound,
    primitive: jax.core.Primitive, *args, **kwargs
    ) -> RelaxVariable:
  """Generates the relaxation for a given primitive op.

  Args:
    index: Integer identifying the computation node.
    out_bounds: Concrete bounds on the outputs of the primitive op.
    primitive: jax primitive
    *args: Arguments of the primitive, wrapped as RelaxVariables
    **kwargs: Keyword Arguments of the primitive.
  Returns:
    `RelaxVariable` that contains the output of this primitive for the
    relaxation, with all the constraints linking the output to inputs.
  """
  # Create variable for output of this primitive
  out_variable = RelaxVariable(index, out_bounds)
  # Create constraints linking output and input of primitive
  constraints = []
  if primitive in _passthrough_list:
    invar = args[0]
    constraints = [RelaxActivationConstraint(out_variable,
                                             invar,
                                             jnp.ones_like(invar.lower),
                                             jnp.zeros_like(invar.lower),
                                             0)]
  elif primitive in _affine_primitives_list:
    results = _get_linear(primitive, out_bounds.lower[0, ...],
                          *args, **kwargs)
    for i, (bias, coeffs) in enumerate(results):
      # Coefficients of the input variable(s).
      vars_and_coeffs = [
          (arg, coeff) for arg, coeff in zip(args, coeffs)
          if isinstance(arg, RelaxVariable)]
      # Equate with the output variable, by using a coefficient of -1.
      out_coeff = (np.array([i], dtype=np.int64), np.array([-1.]))
      vars_and_coeffs.append((out_variable, out_coeff))
      constraints.append(LinearConstraint(vars_and_coeffs, bias, 0))
  elif primitive in _activation_list:
    if len(args) == 2:
      # Generate relu relaxation
      # Relu is implemented as max(x, 0) and both are treated as arguments
      # We find the one that is a RelaxVariable (the other would just be
      # a JAX literal)
      if isinstance(args[0], RelaxVariable):
        invar = args[0]
        if jnp.amax(jnp.abs(args[1])) != 0.:
          raise NotImplementedError('Unsupported activation function')
      elif isinstance(args[1], RelaxVariable):
        invar = args[1]
        if jnp.amax(jnp.abs(args[0])) != 0.:
          raise NotImplementedError('Unsupported activation function')
      else:
        raise NotImplementedError('Activations with multiple arguments not'
                                  'supported.')
      slope, bias = _get_relu_relax(invar.lower, invar.upper)
      constraints += [
          RelaxActivationConstraint(out_variable,
                                    invar,
                                    jnp.zeros_like(invar.lower),
                                    jnp.zeros_like(invar.lower),
                                    1),  # relu(x) >= 0
          RelaxActivationConstraint(out_variable,
                                    invar,
                                    jnp.ones_like(invar.lower),
                                    jnp.zeros_like(invar.lower),
                                    1),  # relu(x) >= x
          RelaxActivationConstraint(out_variable,
                                    invar,
                                    slope,
                                    bias,
                                    -1)]  # upper chord of triangle relax
    else:
      raise NotImplementedError('Activations with multiple arguments not'
                                'supported.')
  out_variable.set_constraints(constraints)
  return out_variable


class RelaxationTransform(bound_propagation.GraphTransform[RelaxVariable]):
  """Transform to produce `RelaxVariable`s for each op."""

  def __init__(self, boundprop_transform: bound_propagation.BoundTransform):
    """Defines relaxation constraint propagation.

    Args:
      boundprop_transform: Basic Jax primitive ops' equivalents for
        the underlying bound propagation method.
    """
    self._boundprop_transform = boundprop_transform

  def input_transform(self, index, lower_bound, upper_bound):
    in_bounds = self._boundprop_transform.input_transform(
        index, lower_bound, upper_bound)
    return _relax_input(index, in_bounds)

  def primitive_transform(self, index, primitive, *args, **params):
    interval_args = [arg.base_bound if isinstance(arg, RelaxVariable) else arg
                     for arg in args]
    out_bounds = self._boundprop_transform.primitive_transform(
        index, primitive, *interval_args, **params)
    return _relax_primitive(index, out_bounds, primitive, *args, **params)


_PRIMITIVES_TO_OPTIMIZE_INPUT = (lax.max_p,)


class OptimizedRelaxationTransform(
    bound_propagation.GraphTransform[RelaxVariable]):
  """Wraps a RelaxVariable-producing BoundTransform to add optimization."""

  def __init__(
      self,
      transform: bound_propagation.GraphTransform[RelaxVariable],
      solver_ctor: Callable[[], RelaxationSolver]):
    self._transform = transform
    self.solver_ctor = solver_ctor
    self.solvers: List[RelaxationSolver] = []

  def tightened_variable_bounds(self, variable: RelaxVariable) -> RelaxVariable:
    """Compute tighter bounds based on the LP relaxation.

    Args:
      variable: Variable as created by the base boundprop transform. This is a
        RelaxVariable that has already been encoded into the solvers.
    Returns:
      tightened_variable: New variable, with the same name, referring to the
        same activation but whose bounds have been optimized by the LP solver.
    """
    lbs = []
    ubs = []
    for solver in self.solvers:
      nb_targets = np.prod(variable.shape[1:])
      sample_lbs = []
      sample_ubs = []
      for target_idx in range(nb_targets):
        objective = (jnp.arange(nb_targets) == target_idx).astype(jnp.float32)
        lb, optimal_lb = solver.minimize_objective(
            variable.name, objective, 0., 0)
        assert optimal_lb
        neg_ub, optimal_ub = solver.minimize_objective(
            variable.name, -objective, 0., 0)
        assert optimal_ub
        sample_lbs.append(lb)
        sample_ubs.append(-neg_ub)
      lbs.append(sample_lbs)
      ubs.append(sample_ubs)

    tightened_base_bound = ibp.IntervalBound(
        jnp.reshape(jnp.array(lbs), variable.shape),
        jnp.reshape(jnp.array(ubs), variable.shape))
    tightened_variable = RelaxVariable(variable.name, tightened_base_bound)
    tightened_variable.set_constraints(variable.constraints)
    # TODO Now that we have obtained tighter bounds, we could make the
    # decision to encode them into the LP solver, which might make the problems
    # easier to solve. This howevere would not change the strength of the
    # relaxation.
    return tightened_variable

  def input_transform(self, index: int,
                      lower_bound: Tensor,
                      upper_bound: Tensor) -> RelaxVariable:
    in_bounds = self._transform.input_transform(
        index, lower_bound, upper_bound)
    for minibatch_index in range(in_bounds.shape[0]):
      # Create one solver instance for each problem in the batch because they
      # will have different constraints.
      if minibatch_index >= len(self.solvers):
        self.solvers.append(self.solver_ctor())
      solver = self.solvers[minibatch_index]
      solver.create_solver_variable(in_bounds, minibatch_index)
    return in_bounds

  def primitive_transform(self,
                          index: int,
                          primitive: jax.core.Primitive,
                          *args: Union[RelaxVariable, Tensor],
                          **params) -> RelaxVariable:
    inp_args = args
    if primitive in _PRIMITIVES_TO_OPTIMIZE_INPUT:
      # This is an activation, so we want to have tight bounds for it.
      # We will take the inputs and tighten all the bounds.
      inp_args = [self.tightened_variable_bounds(inp)
                  if isinstance(inp, RelaxVariable) else inp
                  for inp in args]

    out_bounds = self._transform.primitive_transform(
        index, primitive, *inp_args, **params)
    for minibatch_index, solver in enumerate(self.solvers):
      # Encode the new variable and the associated constraints.
      solver.create_solver_variable(out_bounds, minibatch_index)
      if out_bounds.constraints:
        for constraint in out_bounds.constraints:
          constraint.encode_into_solver(solver, minibatch_index)
    return out_bounds