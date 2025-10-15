# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Fixed
- **Deployment detection**: Fixed critical issue where script would create new deployments even when an active deployment existed on the blockchain. Three fixes were applied:
  1. Wallet is now restored BEFORE checking for deployments (was checked after, causing `self.wallet_address` to be `None`)
  2. Added blockchain query fallback - if local state file is missing or points to closed deployment, script now queries blockchain for any active deployments
  3. When reconstructing deployment from blockchain, script now queries lease information to get provider/gseq/oseq details needed for service status queries
- **Service URL retrieval**: Script now properly retrieves service URL and generates API credentials with actual URL (not placeholder) when using existing deployments found on blockchain

## [1.0.1] - 2025-10-13

### Fixed
- **Certificate file management**: Fixed deployment creation failure when wallet restored from backup in container environment. Script now properly manages certificate file throughout its lifecycle:
  - Restores `.pem` file from Storj backup during wallet restoration
  - Checks for both on-chain certificate AND local certificate file (`~/.akash/[address].pem`)
  - Regenerates local file if on-chain cert exists but local file is missing
  - Creates unified backup (wallet JSON + `.pem` file) and uploads to Storj after new certificate creation
  - Removes certificate file during wallet cleanup
  - This prevents "could not open certificate PEM file" errors during deployment creation
- **Corrected certificate file handling**: Removed references to non-existent `.crt` files (Akash only uses `.pem` files)

### Added
- **Unified wallet backup**: New `create_wallet_backup()` method creates tar.gz archive with wallet mnemonic + certificate file and uploads to Storj, maintaining compatibility with existing backup format
- **Enhanced dry-run certificate checking**: Dry-run mode now checks both local `.pem` file existence AND on-chain certificate status, reporting detailed information in logs and output without making any changes
- **Version bump script**: New `bump-version.sh` helper script automates version bumping, CHANGELOG updates, git tagging, and GitHub release creation
- **Development documentation**: Added VERSION-BUMP-SCRIPT.md with detailed usage guide
- New WORKFLOW.md documenting simple git workflow (commit early and often)

### Improved
- **Backup timing**: Unified backup is now created AFTER successful certificate publish (not before), ensuring we only backup if the publish succeeds (which costs AKT gas fees)
- Added test-env-vars.sh.example as template for environment variables
- Added examples/ directory with Akash SDL deployment example

### Changed
- Simplified git workflow to focus on frequent commits and clear documentation

### Improved
- RPC node selection logging now uses proper logger instead of print statements
- Logger is initialized before RPC node testing for better output
- RPC node test results are now logged with proper info/warning levels

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
