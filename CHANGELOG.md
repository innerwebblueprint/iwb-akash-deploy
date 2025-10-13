# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- New WORKFLOW.md documenting simple git workflow (commit early and often)
- Added .gitignore to protect secrets and exclude generated files
- Added test-env-vars.sh.example as template for environment variables
- Added examples/ directory with ComfyUI deployment example

### Changed
- Simplified git workflow to focus on frequent commits and clear documentation
- Removed complex automated versioning tools (Commitizen) in favor of manual semantic versioning
- Cleaned up pyproject.toml, removed unnecessary Commitizen configuration
- Updated .gitignore to exclude personal todo.md file

### Improved
- RPC node selection logging now uses proper logger instead of print statements
- Logger is initialized before RPC node testing for better output
- RPC node test results are now logged with proper info/warning levels

### Removed
- Removed .commitlintrc.json (commit linting config)
- Removed .github/workflows/ automated release workflows
- Removed .github/COMMIT_CONVENTION.md (overly complex)
- Removed notes/RELEASE.md (complex release documentation)
- Removed notes/GIT-COMMIT-GUIDE.md (overly verbose guide)

## [1.0.0] - 2025-10-12

### Added
- Initial release of iwb-akash-deploy
- Complete Akash deployment orchestration for IWBDPP
- Wallet management with automatic recovery from encrypted storage
- Akash Certificate management
- Deployment lifecycle management (create, bid selection, lease management, close)
- Provider blocklist support for avoiding unreliable providers
- GPU preference and priority-based bid selection
- Service URL extraction and API credential generation
- Clean JSON output for n8n workflow integration
- Comprehensive error handling and logging
- State file management for deployment persistence
- Support for existing deployment detection and reuse
