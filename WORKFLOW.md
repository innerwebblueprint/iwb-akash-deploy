# Simple Git Workflow

## Daily Development

### 1. Make Changes & Commit Often

```bash
# Make your code changes
# Then commit with a short message
git add .
git commit -m "fix bid selection logic"
git push
```

**Commit message guidelines:**
- Keep it short (50 chars or less)
- Use present tense ("fix" not "fixed")
- Start with lowercase
- Common prefixes: `fix`, `add`, `update`, `remove`, `refactor`, `docs`

### 2. Update CHANGELOG as You Go

After each commit (or group of related commits), document the **why** and **what** in CHANGELOG.md:

```markdown
## [Unreleased]

### Fixed
- Bid selection now correctly prioritizes GPU providers
  - Was selecting first bid instead of best match
  - Added sorting by price and GPU attributes
  - Fixes issue where CPU-only providers were chosen for GPU workloads
```

**Keep it human-readable:** Explain the problem, the solution, and the impact.

### 3. Create a Release When Ready

When you have a working, tested feature:

```bash
# 1. Make sure CHANGELOG.md is updated
# 2. Decide version bump (see Versioning below)
# 3. Update version in CHANGELOG.md header:

## [1.0.1] - 2025-10-13

# 4. Create and push the tag
git add CHANGELOG.md
git commit -m "release v1.0.1"
git tag -a v1.0.1 -m "Release v1.0.1"
git push && git push --tags
```

## Versioning (Semantic Versioning)

Given a version number MAJOR.MINOR.PATCH (e.g., 1.2.3):

- **PATCH** (1.0.1 → 1.0.2): Bug fixes, small changes
- **MINOR** (1.0.0 → 1.1.0): New features, backwards compatible
- **MAJOR** (1.0.0 → 2.0.0): Breaking changes, API changes

**Most releases will be PATCH or MINOR.**

## CHANGELOG.md Format

```markdown
# Changelog

## [Unreleased]
<!-- Work in progress, not yet released -->

### Added
- New features

### Fixed  
- Bug fixes

### Changed
- Changes to existing functionality

### Removed
- Removed features

## [1.0.1] - 2025-10-13

### Fixed
- Detailed description of what was fixed and why

## [1.0.0] - 2025-10-12

Initial release
```

## Examples

### Example: Quick Bug Fix

```bash
# 1. Fix the bug
# 2. Commit
git add iwb-akash-deploy.py
git commit -m "fix wallet recovery error handling"
git push

# 3. Update CHANGELOG.md
# Add entry under [Unreleased] > ### Fixed

# 4. When tested and ready
# Move to versioned section and release
```

### Example: Adding New Feature

```bash
# 1. Implement feature over multiple commits
git commit -m "add initial provider scoring"
git commit -m "add tests for provider scoring"
git commit -m "integrate scoring into bid selection"

# 2. Document in CHANGELOG under [Unreleased] > ### Added

# 3. Test thoroughly

# 4. Release as minor version bump (1.0.0 → 1.1.0)
```

## That's It!

No complicated tools, no automated versioning systems, no commit conventions to memorize. Just:

1. **Commit often** with short messages
2. **Document in CHANGELOG** with details
3. **Release when ready** with semantic versioning

Simple, effective, and keeps you moving fast.
