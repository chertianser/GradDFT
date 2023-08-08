from jax.random import PRNGKey
from jax.lax import stop_gradient
from evaluate import make_scf_loop

from interface.pyscf import molecule_from_pyscf
from functional import DM21

# In this example we aim to explain how we can implement the self-consistent loop
# with the DM21 functional.

# First we define a molecule, using pyscf:
from pyscf import gto, dft
mol = gto.M(atom = 'H 0 0 0; F 0 0 1.1')

grids = dft.gen_grid.Grids(mol)
grids.level = 2
grids.build()

mf = dft.UKS(mol)
mf.grids = grids
ground_truth_energy = mf.kernel()

# Then we compute quite a few properties which we pack into a class called Molecule.
# omegas will indicate the values of w in the range-separated Coulomb kernel
#  erf(w|r-r'|)/|r-r'|.
# Note that w = 0 indicates the usual Coulomb kernel 1/|r-r'|.
molecule = molecule_from_pyscf(mf, omegas = [0., 0.4])

functional = DM21()
params = functional.generate_DM21_weights()

key = PRNGKey(42) # Jax-style random seed

# We generate the input densities from the molecule we created before
grad_densities = functional.densities(molecule)
nograd_densities = stop_gradient(functional.nograd_densities(molecule))
densities = functional.combine_densities(grad_densities, nograd_densities)

# We now generated the inputs to the coefficients nn
grad_cinputs = functional.coefficient_inputs(molecule)
nograd_cinputs = stop_gradient(functional.nograd_coefficient_inputs(molecule))
coefficient_inputs = functional.combine_inputs(grad_cinputs, nograd_cinputs)

# And then we compute the energy
predicted_energy = functional.apply_and_integrate(params, molecule.grid, coefficient_inputs, densities)
predicted_energy += molecule.nonXC()
print('Predicted_energy (detailed code):',predicted_energy)

# Alternatively, we can use an already prepared function that does everything
predicted_energy = functional.energy(params, molecule)
print('Predicted_energy:',predicted_energy)

# Finally, we create and implement the self-consistent loop.
scf_iterator = make_scf_loop(functional, verbose = 2, max_cycles = 5)
predicted_e = scf_iterator(params, molecule)

print(f'The predicted energy is {predicted_e}')