from options import RuntimeOptions


def build_command(target, options=None):
    options = options or RuntimeOptions()
    command = f"deploy {target}"
    if options.verbose:
        command += " --verbose"
    return "echo " + command if options.dry_run else command
