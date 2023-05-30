from functools import partial
from jax.random import split, PRNGKey
from jax import numpy as jnp
from optax import adam, apply_updates
from evaluate import make_molecule_scf_loop

from interface.pyscf import molecule_from_pyscf
from molecule import default_features, features_w_hf
from functional import DM21, default_loss
from jax.experimental import checkify

# First we define a molecule:
from pyscf import gto, dft
mol = gto.M(atom = 'H 0 0 0; F 0 0 1.1')

grids = dft.gen_grid.Grids(mol)
grids.level = 2
grids.build()

mf = dft.UKS(mol)
mf.grids = grids
ground_truth_energy = mf.kernel()

# Then we compute quite a few properties which we pack into a class called Molecule
molecule = molecule_from_pyscf(mf, omegas = [1e20, 0.4])

functional = DM21()
params = functional.generate_DM21_weights()

key = PRNGKey(42) # Jax-style random seed

# We generate the features from the molecule we created before
omegas = molecule.omegas
feature_fn = default_features

for omega in omegas:
    assert omega in molecule.omegas, f"omega {omega} not in the molecule.omegas"
if len(omegas) > 0:
    feature_fn_w_hf = partial(features_w_hf, features = feature_fn)
    functional_inputs = feature_fn_w_hf(molecule)
else:
    functional_inputs = feature_fn(molecule)

energy = functional.apply_and_integrate(params, molecule, *functional_inputs)
energy += molecule.nonXC()

# Alternatively, we can use an already prepared function that does everything
predicted_energy = functional.energy(params, molecule, *functional_inputs)
print('Predicted_energy:',predicted_energy)
# If we had a non-local functional, eg whose function f outputs an energy instead of an array,
# we'd just avoid the integrate step.

scf_iterator = make_molecule_scf_loop(functional, feature_fn=default_features, verbose = 2, omegas = molecule.omegas, functional_type = "DM21")
predicted_e = scf_iterator(params, molecule)

print(f'The predicted energy is {energy}')