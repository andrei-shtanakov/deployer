"""Job that proves the 'cli' extra is installed at run time."""

if __name__ == "__main__":
    import cowsay  # noqa: F401  # ImportError here = extra not installed

    print("hello from extras-job")
