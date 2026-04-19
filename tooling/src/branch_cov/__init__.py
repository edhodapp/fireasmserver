"""Assembly branch-coverage tooling for fireasmserver.

Enumerate every conditional branch in a guest ELF, compare against the
PC execution trace from a test run, and report any (addr, outcome)
pair that was never observed. Fails the gate if any gap exists.
"""
