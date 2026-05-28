"""Homeostatski Motor — računa entropiju (bol) iz senzornog stanja."""
from .config import (
    THRESHOLD_CPU, THRESHOLD_RAM, THRESHOLD_DISK,
    WEIGHT_CPU, WEIGHT_RAM, WEIGHT_DISK, WEIGHT_LOAD, WEIGHT_SWAP,
)


class EntropyEngine:
    def calculate(self, state):
        entropy = 0.0
        cpu = state.get("cpu_percent", 0.0)
        ram = state.get("ram_percent", 0.0)
        disk = state.get("disk_percent", 0.0)
        swap = state.get("swap_percent", 0.0)
        load = state.get("load_1min", 0.0)
        zombies = state.get("zombie_count", 0)
        services_down = len(state.get("services_down", []))

        if cpu > THRESHOLD_CPU:
            entropy += (cpu - THRESHOLD_CPU) * WEIGHT_CPU
        if ram > THRESHOLD_RAM:
            entropy += (ram - THRESHOLD_RAM) * WEIGHT_RAM
        if disk > THRESHOLD_DISK:
            entropy += (disk - THRESHOLD_DISK) * WEIGHT_DISK
        if swap > 50.0:
            entropy += (swap - 50.0) * WEIGHT_SWAP
        if load > 4.0:
            entropy += (load - 4.0) * WEIGHT_LOAD
        if zombies > 10:
            entropy += (zombies - 10) * 0.1
        if services_down > 0:
            entropy += services_down * 2.0

        return round(entropy, 2)

    def identify_sources(self, state):
        sources = []
        if state.get("cpu_percent", 0.0) > THRESHOLD_CPU:
            sources.append("high_cpu")
        if state.get("ram_percent", 0.0) > THRESHOLD_RAM:
            sources.append("high_ram")
        if state.get("disk_percent", 0.0) > THRESHOLD_DISK:
            sources.append("high_disk")
        if state.get("swap_percent", 0.0) > 50.0:
            sources.append("high_swap")
        if state.get("load_1min", 0.0) > 4.0:
            sources.append("high_load")
        if state.get("zombie_count", 0) > 10:
            sources.append("zombie_processes")
        if state.get("services_down", []):
            sources.append("service_down")
        return sources
