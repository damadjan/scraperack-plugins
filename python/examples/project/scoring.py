"""A local module the 06_working_dir example imports inside the microVM."""


def rank(words):
    return sorted(set(words), key=lambda w: (-len(w), w))[:5]
