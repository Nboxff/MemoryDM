"""Settings for the published retrieval comparisons."""

from dataclasses import replace

VARIANTS = ("configured", "full", "no_hybrid", "no_grep")


def add_variant_arguments(parser):
    parser.add_argument("--variant", choices=VARIANTS, default="configured")
    parser.add_argument("--with-derived", action="store_true",
                        help="Enable experience memory and user profiles")


def apply_variant(settings, variant, *, with_derived=False):
    if variant not in VARIANTS:
        raise ValueError("Unknown experiment variant: " + variant)
    if with_derived:
        settings = replace(settings, experience_enabled=True,
                           experience_retrieval_enabled=True, profile_enabled=True)
    methods = {"full": ("hybrid", "grep", "time"),
               "no_hybrid": ("grep", "time"), "no_grep": ("hybrid", "time")}
    if variant in methods:
        return replace(settings, retrieval_methods=methods[variant])
    return settings
