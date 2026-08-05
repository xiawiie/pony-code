from options import RuntimeOptions


def build_command(target, options=None):
    options = options or RuntimeOptions()
    command = f"deploy {target}"
    return command + (" --verbose" if options.verbose else "")
