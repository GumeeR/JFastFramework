"""Built-in plugins.

These are registered through the same ``jfastframework.plugins`` entry-point
group that third-party plugins use, so nothing here is privileged. A service
can disable any of them, and a third-party plugin can replace one by claiming
the same provider key after the built-in is disabled.

Import them lazily -- most carry optional dependencies.
"""

__all__: list[str] = []
