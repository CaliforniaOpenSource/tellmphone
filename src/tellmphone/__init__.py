"""TeLLMphone — let your LLMs call each other."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tellmphone")
except PackageNotFoundError:  # source tree without an install
    __version__ = "0+unknown"
