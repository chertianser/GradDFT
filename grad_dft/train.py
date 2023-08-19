# Copyright 2023 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Callable, Optional, Tuple
from functools import partial

from jax import jit, numpy as jnp, vmap
from jax import value_and_grad
from jax.profiler import annotate_function
from jax.lax import stop_gradient, cond, fori_loop
from flax import struct
from optax import OptState, GradientTransformation, apply_updates

from grad_dft.utils import Scalar, Array, PyTree
from grad_dft.functional import DispersionFunctional, Functional
from grad_dft.molecule import Molecule, coulomb_potential, symmetrize_rdm1


def molecule_predictor(
    functional: Functional,
    nlc_functional: DispersionFunctional = None,
    **kwargs,
) -> Callable:
    r"""Generate a function that predicts the energy
    energy of a `Molecule` and a corresponding Fock matrix

    Parameters
    ----------
    functional : Functional
        A callable or a `flax.linen.Module` that predicts the
        exchange-correlation energy given some parameters.
        A callable must have the following signature:

        fxc.energy(params: Array, molecule: Molecule, **functional_kwargs) -> Scalar

        where `params` is any parameter pytree, and `molecule`
        is a Molecule class instance.

    Returns
    -------
    Callable
        A wrapped verison of `fxc` that calculates input/output features and returns
        the predicted energy with the corresponding Fock matrix.
        Signature:

        (params: PyTree, molecule: Molecule, *args) -> Tuple[Scalar, Array]

    Notes
    -----
    In a nutshell, this takes any Jax-transformable functional and does two things:
        1.) Wraps it in a way to return the Fock matrix as well as
        the energy.
        2.) Explicitly takes a function to generate/load precomputed
        features to feed into a parameterized functional for flexible
        feature generation.

    Examples
    --------
    Given a `Molecule`:
    >>> from qdft import FeedForwardFunctional
    >>> Fxc = FeedForwardFunctional(layer_widths=[128, 128])
    >>> params = Fxc.init(jax.random.PRNGKey(42), jnp.zeros(shape=(32, 11)))
    >>> predictor = make_molecule_predictor(Fxc, chunk_size=1000)
    `chunk` size is forwarded to the default feature function as a keyword parameter.
    >>> e, fock = predictor(params, molecule) # `Might take a while for the default_features`
    >>> fock.shape == molecule.density_matrix.shape
    True
    """

    @partial(value_and_grad, argnums=1)
    def energy_and_grads(
        params: PyTree, rdm1: Array, molecule: Molecule, *args, **functional_kwargs
    ) -> Scalar:
        r"""
        Computes the energy and gradients with respect to the density matrix

        Parameters
        ----------
        params: Pytree
            Functional parameters
        rdm1: Array
            The reduced density matrix.
            Expected shape: (n_grid_points, n_orbitals, n_orbitals)
        molecule: Molecule
            the molecule

        Returns
        -----------
        Scalar
            The energy of the molecule when the state of the system is given by rdm1.
        """

        molecule = molecule.replace(rdm1=rdm1)

        e = functional.energy(params, molecule, *args, **functional_kwargs)
        if nlc_functional:
            e = e + nlc_functional.energy(
                {"params": params["dispersion"]}, molecule, **functional_kwargs
            )
        return e

    @partial(annotate_function, name="predict")
    def predict(params: PyTree, molecule: Molecule, *args) -> Tuple[Scalar, Array]:
        r"""A DFT functional wrapper, returning the predicted exchange-correlation
        energy as well as the corresponding Fock matrix. This function does **not** require
        that the provided `feature_fn` returns derivatives (Jacobian matrix) of provided
        input features.

        Parameters
        ----------
        params : PyTree
            The functional parameters.
        molecule : Molecule
            The `Molecule` object to predict properties of.
        *args

        Returns
        -------
        Tuple[Scalar, Array]
            A tuple of the predicted exchange-correlation energy and the corresponding
            Fock matrix of the same shape as `molecule.density_matrix`:
            (*batch_size, n_spin, n_orbitals, n_orbitals).
        """

        energy, fock = energy_and_grads(params, molecule.rdm1, molecule, *args)
        fock = 1 / 2 * (fock + fock.transpose(0, 2, 1))

        # Compute the features that should be autodifferentiated
        if functional.energy_densities and functional.densitygrads:
            grad_densities = functional.energy_densities(molecule, *args, **kwargs)
            nograd_densities = stop_gradient(functional.nograd_densities(molecule, *args, **kwargs))
            densities = functional.combine_densities(grad_densities, nograd_densities)
        elif functional.energy_densities:
            grad_densities = functional.energy_densities(molecule, *args, **kwargs)
            nograd_densities = None
            densities = grad_densities
        elif functional.densitygrads:
            grad_densities = None
            nograd_densities = stop_gradient(functional.nograd_densities(molecule, *args, **kwargs))
            densities = nograd_densities
        else:
            densities, grad_densities, nograd_densities = None, None, None

        if functional.coefficient_input_grads and functional.coefficient_inputs:
            grad_cinputs = functional.coefficient_inputs(molecule, *args, **kwargs)
            nograd_cinputs = stop_gradient(
                functional.nograd_coefficient_inputs(molecule, *args, **kwargs)
            )
            cinputs = functional.combine_inputs(grad_cinputs, nograd_cinputs)
        elif functional.coefficient_inputs:
            grad_cinputs = functional.coefficient_inputs(molecule, *args, **kwargs)
            nograd_cinputs = None
            cinputs = grad_cinputs
        elif functional.coefficient_input_grads:
            grad_cinputs = None
            nograd_cinputs = stop_gradient(
                functional.nograd_coefficient_inputs(molecule, *args, **kwargs)
            )
            cinputs = nograd_cinputs
        else:
            cinputs, grad_cinputs, nograd_cinputs = None, None, None

        # Compute the derivatives with respect to nograd_densities
        if functional.densitygrads:
            vxc_expl = functional.densitygrads(
                functional, params, molecule, nograd_densities, cinputs, grad_densities
            )
            fock += vxc_expl + vxc_expl.transpose(0, 2, 1)  # Sum over omega

        if functional.coefficient_input_grads:
            vxc_expl = functional.coefficient_input_grads(
                functional, params, molecule, nograd_cinputs, grad_cinputs, densities
            )
            fock += vxc_expl + vxc_expl.transpose(0, 2, 1)  # Sum over omega

        if functional.is_xc:
            rdm1 = symmetrize_rdm1(molecule.rdm1)
            fock += coulomb_potential(rdm1, molecule.rep_tensor)
            # fock = cond(jnp.isclose(molecule.spin, 0), # Condition
            #                lambda x: x, # Truefn branch
            #                lambda x: jnp.stack([x.sum(axis = 0)/2., x.sum(axis = 0)/2.], axis=0), # Falsefn branch
            #                fock) # Argument
            fock = fock + jnp.stack([molecule.h1e, molecule.h1e], axis=0)

        return energy, fock

    return predict


def make_train_kernel(tx: GradientTransformation, loss: Callable) -> Callable:
    def kernel(
        params: PyTree, opt_state: OptState, system: Molecule, ground_truth_energy: float, *args
    ) -> Tuple[PyTree, OptState, Scalar, Scalar]:
        (cost_value, predictedenergy), grads = loss(params, system, ground_truth_energy)

        updates, opt_state = tx.update(grads, opt_state, params)
        params = apply_updates(params, updates)

        return params, opt_state, cost_value, predictedenergy

    return kernel


##################### Regularization #####################


def fock_grad_regularization(molecule: Molecule, F: Array) -> Scalar:
    """Calculates the Fock alternative regularization term for a `Molecule` given a Fock matrix.

    Parameters
    ----------
    molecule : Molecule
        A `Molecule` object.
    F : Array
        The Fock matrix array. Has to be of the same shape as `molecule.density_matrix`

    Returns
    -------
    Scalar
        The Fock gradient regularization term alternative.
    """
    return jnp.sqrt(jnp.einsum("sij->", (F - molecule.fock) ** 2)) / jnp.sqrt(
        jnp.einsum("sij->", molecule.fock**2)
    )


def dm21_grad_regularization(molecule: Molecule, F: Array) -> Scalar:
    """Calculates the default gradient regularization term for a `Molecule` given a Fock matrix.

    Parameters
    ----------
    molecule : Molecule
        A `Molecule` object.
    F : Array
        The Fock matrix array. Has to be of the same shape as `molecule.density_matrix`

    Returns
    -------
    Scalar
        The gradient regularization term of the DM21 variety.
    """

    n = molecule.mo_occ
    e = molecule.mo_energy
    C = molecule.mo_coeff

    # factors = jnp.einsum("sba,sac,scd->sbd", C.transpose(0,2,1), F, C) ** 2
    # factors = jnp.einsum("sab,sac,scd->sbd", C, F, C) ** 2
    # factors = jnp.einsum("sac,sab,scd->sbd", F, C, C) ** 2
    factors = jnp.einsum("sac,sab,scd->sbd", F, C, C) ** 2  # F is symmetric

    numerator = n[:, :, None] - n[:, None, :]
    denominator = e[:, :, None] - e[:, None, :]

    mask = jnp.logical_and(jnp.abs(factors) > 0, jnp.abs(numerator) > 0)

    safe_denominator = jnp.where(mask, denominator, 1.0)

    second_mask = jnp.abs(safe_denominator) > 0
    safe_denominator = jnp.where(second_mask, safe_denominator, 1.0e-20)

    prefactors = numerator / safe_denominator

    dE = jnp.clip(0.5 * jnp.sum(prefactors * factors), a_min=-10, a_max=10)

    return dE**2


def orbital_grad_regularization(molecule: Molecule, F: Array) -> Scalar:
    """Deprecated"""

    #  Calculate the gradient regularization term
    new_grad = get_grad(molecule.mo_coeff, molecule.mo_occ, F)

    dE = jnp.linalg.norm(new_grad - molecule.training_gorb_grad, ord="fro")

    return dE**2


def get_grad(mo_coeff, mo_occ, F):
    """RHF orbital gradients

    Args:
        mo_coeff: 2D ndarray
            Orbital coefficients
        mo_occ: 1D ndarray
            Orbital occupancy
        F: 2D ndarray
            Fock matrix in AO representation

    Returns:
        Gradients in MO representation.  It's a num_occ*num_vir vector.

    # Similar to pyscf/scf/hf.py:
    occidx = mo_occ > 0
    viridx = ~occidx
    g = reduce(jnp.dot, (mo_coeff[:,viridx].conj().T, fock_ao,
                           mo_coeff[:,occidx])) * 2
    return g.ravel()
    """

    C_occ = vmap(jnp.where, in_axes=(None, 1, None), out_axes=1)(mo_occ > 0, mo_coeff, 0)
    C_vir = vmap(jnp.where, in_axes=(None, 1, None), out_axes=1)(mo_occ == 0, mo_coeff, 0)

    return jnp.einsum("sab,sac,scd->bd", C_vir.conj(), F, C_occ)