"""Checks for the VM memory sizing formula. Run: uv run python test/test_vm_memory.py"""

from locki.services.vm import vm_memory_gib

# host total GiB -> expected guest GiB
EXPECTED = {3: 2, 4: 2, 8: 6, 16: 14, 32: 28, 117: 103}

for total, guest in EXPECTED.items():
    assert vm_memory_gib(total) == guest, f"total={total}: got {vm_memory_gib(total)}, want {guest}"

for total in range(1, 1025):
    got = vm_memory_gib(total)
    assert got >= 2, f"total={total}: guest {got} below 2 GiB floor"
    assert total <= 4 or got < total, f"total={total}: no headroom reserved (guest {got})"

print("test_vm_memory: OK")
