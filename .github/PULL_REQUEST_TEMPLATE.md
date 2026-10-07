# Pull Request Template

## Description

<!-- Describe what this PR does and why. Link to related issue if applicable. -->

Fixes #<!-- issue number -->

## Type of Change

<!-- Check all that apply -->

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update
- [ ] Refactor / code cleanup (no functional change)
- [ ] Performance improvement
- [ ] Test addition / improvement
- [ ] CI / build / configuration change

## Changes Made

<!-- List the key changes in this PR -->

-
-
-

## Testing

<!-- Describe how you tested this change -->

- [ ] All existing tests pass (`pytest tests -v`)
- [ ] New tests added for new functionality
- [ ] Manual testing performed (describe below)
- [ ] Linting passes (`ruff check src tests`)
- [ ] Type checking passes (`mypy src`)
- [ ] Formatting passes (`ruff format --check src tests`)

### Manual Test Plan

<!-- Steps to manually verify the change -->

1.
2.
3.

## Configuration Impact

<!-- Does this change config.yaml, CLI flags, or environment variables? -->

- [ ] No config changes
- [ ] New config option(s) added (documented in config.yaml comments)
- [ ] Existing config option(s) modified (breaking change?)
- [ ] New CLI flag(s) added
- [ ] New environment variable(s) supported

## Documentation

<!-- Check all that apply -->

- [ ] README.md updated
- [ ] Config comments updated (in config.yaml or DEFAULTS in src/__init__.py)
- [ ] Docstrings added/updated for public functions
- [ ] CHANGELOG.md updated under `## [Unreleased]`
- [ ] No documentation needed

## Screenshots / Logs

<!-- If applicable, add screenshots or relevant log output -->

```
# Paste relevant output here
```

## Checklist

<!-- Final verification before requesting review -->

- [ ] My code follows the project's style guidelines (Ruff, MyPy)
- [ ] I have performed a self-review of my own code
- [ ] I have commented my code, particularly in hard-to-understand areas
- [ ] I have made corresponding changes to the documentation
- [ ] My changes generate no new warnings or errors
- [ ] Any dependent changes have been merged and published
- [ ] I have squashed fixup commits (or will allow maintainer to squash merge)

## Additional Notes

<!-- Any other information for reviewers -->