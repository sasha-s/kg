"""Build Cython extension for fast CDC chunking (optional — falls back to pure Python)."""

from setuptools import setup
from setuptools.command.build_ext import build_ext


class BuildExtWithFallback(build_ext):
    """Build extension with fallback to pure Python on failure."""

    def run(self):
        try:
            build_ext.run(self)
        except Exception as e:
            print(f"Warning: Failed to build Cython extension: {e}")
            print("Falling back to pure Python fastcdc (slower but fully functional)")

    def build_extensions(self):
        try:
            super().build_extensions()
        except Exception as e:
            print(f"Warning: Failed to build Cython extension: {e}")
            print("Falling back to pure Python fastcdc (slower but fully functional)")


def get_ext_modules():
    try:
        from Cython.Build import cythonize
        return cythonize(
            ["src/kg/_vendor/fastcdc/fastcdc_cy.pyx"],
            language_level=3,
        )
    except ImportError:
        print("Cython not available, skipping fastcdc_cy extension build")
        return []


setup(
    ext_modules=get_ext_modules(),
    cmdclass={"build_ext": BuildExtWithFallback},
)
