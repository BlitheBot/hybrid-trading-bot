"""
Auto-discovers all DiscoveryStrategy subclasses in this package.
Drop a new .py file here that defines a class inheriting DiscoveryStrategy
and load_all_strategies() will pick it up automatically — no registration needed.
"""
import importlib
import inspect
import pkgutil
from pathlib import Path

from discovery.strategies.base import DiscoveryStrategy


def load_all_strategies() -> list[type[DiscoveryStrategy]]:
    strategies: list[type[DiscoveryStrategy]] = []
    package_dir = Path(__file__).parent
    for _finder, module_name, _is_pkg in pkgutil.iter_modules([str(package_dir)]):
        if module_name == "base":
            continue
        module = importlib.import_module(f"discovery.strategies.{module_name}")
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, DiscoveryStrategy)
                and obj is not DiscoveryStrategy
                and obj.__module__ == module.__name__
            ):
                strategies.append(obj)
    return strategies
