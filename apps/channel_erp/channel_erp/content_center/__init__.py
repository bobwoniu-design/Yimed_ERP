"""Public facade for the independent, company-neutral Content Center."""

from . import simple_api as _simple


def __getattr__(name):
	return getattr(_simple, name)
