"""Use synthetic adapters unless a test explicitly selects another app mode."""
import os


os.environ["APP_ENV"] = "test"
