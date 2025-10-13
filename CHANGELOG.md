# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Changed
- Simplified git workflow to focus on frequent commits and clear documentation
- Removed complex automated versioning tools in favor of manual semantic versioning

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
