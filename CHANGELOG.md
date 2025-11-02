# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- **Modular bid query system**: Created centralized `_query_bids(dseq, state_filter)` method
  - Returns categorized bid results: `open_bids`, `closed_bids`, `all_bids`
  - Supports filtering by state: `'open'`, `'closed'`, or `'all'`
  - Eliminates code duplication across multiple methods
  - Used by both `wait_for_bids()` and deployment validation logic

- **Enhanced deployment-without-lease recovery**: Intelligent handling of deployments that exist but have no lease
  - Automatically queries blockchain for lease info when missing from state
  - Checks bid status (open/closed/none) to determine appropriate action
  - Auto-creates lease if open bids are available
  - Auto-closes deployment if bids expired or deployment is stale (>5 min with no bids)
  - Distinguishes between "never had bids" vs "had bids that expired"
  - Prevents duplicate deployments - always checks for existing deployment first

### Changed
- **Refactored `wait_for_bids()`**: Now uses centralized `_query_bids()` method
  - Removed duplicate bid query logic
  - Simplified code structure for better maintainability
  - Improved debugging output

- **Enhanced `run()` deployment validation**: More robust checking for existing deployments
  - Always verifies deployment state before creating new one
  - Checks for lease info in state file and blockchain
  - Automatic lease creation if deployment exists with open bids
  - Automatic cleanup of expired/stale deployments

### Fixed
- **Deployment-without-lease scenarios**: Script now properly handles all edge cases
  - Scenario 1: Deployment with open bids → Creates lease and sends manifest
  - Scenario 2: Deployment with only closed bids → Closes deployment (bids expired)
  - Scenario 3: Deployment >5 min old with no bids → Closes stale deployment
  - Scenario 4: Deployment <5 min old with no bids → Waits for bids
  - Clear, actionable error messages for each scenario

- **Code duplication**: Eliminated duplicate bid query logic
  - Previously had bid queries in both `_query_bids()` and `wait_for_bids()`
  - Now centralized in single `_query_bids()` method

### Improved
- **Error messaging**: More informative messages for deployment states
  - "Deployment had bids but they all expired" - User knows bids came but timed out
  - "No bids received after 5 minutes" - User knows deployment never attracted bids
  - "Deployment exists but has no bids yet (age: X min)" - User knows to wait
  - Includes bid counts in logs: "📊 Bid status: X open, Y closed, Z total"

## [1.1.7] - 2025-11-01

## [1.1.6] - 2025-11-01

### Changed
- **Akash Mainnet 14 compatibility**: Updated script for Akash Network Mainnet 14 upgrade
  - Verified compatibility with provider-services v0.10.1 (upgraded from v0.8.4)
  - Provider-services v0.10.1 uses new chain SDK and Akash v1 blockchain architecture
  - Updated event parsing to handle new `akash.deployment.v1.EventDeploymentCreated` event structure
  - Updated bid structure: changed from `bid['bid']['bid_id']` to `bid['bid']['id']`
  - All RPC nodes tested and confirmed working with new mainnet version
  - Script now fully compatible with Mainnet 14 network changes (October 2025)
  - **Tested successfully**: Full deployment workflow verified on local machine

### Fixed
- **Code structure**: 
  - Corrected Python file structure: shebang → docstring → imports → functions → classes
  - Fixed PEP 8 compliance: separated multi-import statements onto individual lines
  - Alphabetically sorted imports for better maintainability

- **Bid selection crash**: Fixed `KeyError: 'bid_id'` error when selecting bids
  - Mainnet 14 changed bid structure from `bid['bid']['bid_id']` to `bid['bid']['id']`
  - Updated `select_best_bid()` and `create_lease()` to use new structure
  - Bid selection now works correctly with Mainnet 14 responses
  - **Verified**: Successfully processed 15 bids and selected best provider (RTX 4090)

- **DSEQ parsing bug**: Fixed critical bug where block height was used instead of deployment DSEQ
  - Script was incorrectly parsing `height` field (23989108) instead of actual DSEQ (23989107)
  - Enhanced `_parse_dseq_from_output()` to extract DSEQ from `EventDeploymentCreated` event
  - Removed fallback text parsing - only JSON responses are now used for DSEQ extraction
  - Deployment now correctly tracks the actual deployment, not an off-by-one error
  - This fix was required due to Mainnet 14's new event structure
  - **Verified**: Successfully parsed DSEQ 24004380 from deployment creation

### Tested
- **Complete deployment workflow verified**:
  - ✅ Wallet restoration and balance check
  - ✅ Certificate status verification
  - ✅ Deployment creation with correct DSEQ parsing
  - ✅ Bid reception and processing (15 bids received)
  - ✅ Provider scoring and selection (RTX 4090 selected with score 135.1)
  - ✅ Lease creation with provider
  - ✅ Manifest send to provider
  - ✅ State file management
  - All steps completed successfully in 43 seconds

## [1.1.5] - 2025-10-20

### Fixed
- **GPU preference scoring**: Fixed organization bonus overriding GPU preferences
  - Increased GPU preference score gap from 10 to 30 points per position
  - Reduced organization bonuses (overclock: 20→10, datacenter: 15→5)
  - RTX 4090 now always scores higher than A100, even with organization bonuses
  - New scoring: RTX 4090=100pts, A100=70pts, H100=40pts (before org/location bonuses)
  - Ensures manifest GPU preference order is strictly followed

### Improved
- **Code optimization**: Reduced code by 12 lines while maintaining all functionality
  - Added `_ensure_wallet_and_deployment()` helper method to eliminate repeated wallet/deployment checks
  - Consolidated 7 instances of wallet restoration + deployment checking pattern
  - Simplified error response handling in exception blocks
  - Methods affected: `check_ready()`, `close_deployment()`, `get_lease_status()`, `get_lease_logs()`, `get_interactive_shell()`

## [1.1.4] - 2025-10-20

### Fixed
- **Close deployment JSON output**: Fixed crash when closing deployments
  - Added proper null/type checking before parsing transaction output
  - Script now returns proper JSON for n8n instead of AttributeError traceback
  - Deployment closes successfully even when transaction output is None or unparseable
  - Handles cases where `execute_tx()` returns None for stdout

- **Email notifications**: Re-enabled email notifications for deployments
  - Added email notification when deployment starts (async mode compatible)
  - Email includes DSEQ, provider, API credentials, and timestamp
  - Close deployment emails now work (previously crashed before sending)
  - Both notifications use system `mail` command with IWB_DOMAIN config

## [1.1.3] - 2025-10-20

### Fixed
- **GPU model detection**: Fixed critical bug where all provider GPU models showed as "Unknown"
  - Added `_extract_gpu_model()` method to parse GPU models from Akash attribute keys
  - Akash stores GPU info as keys like `capabilities/gpu/vendor/nvidia/model/a100`, not as values
  - Script was looking for `capabilities/gpu/model` attribute (doesn't exist)
  - GPU-based scoring now works correctly: RTX 4090 (100pts), A100 (90pts), H100 (80pts), etc.
  - Providers are now properly prioritized by GPU model according to manifest preferences
  - Bug caused cheapest US provider to win regardless of GPU type (only location scored)

## [1.1.2] - 2025-10-20

### Added
- **Deployment creation timeout resilience**: RPC timeouts no longer cause deployment failures
  - New `_find_recent_deployment()` method queries blockchain for recently created deployments
  - Retry logic: 3 attempts with 5-second waits (15 seconds total) to find deployment after timeout
  - Distinguishes between RPC timeout (transaction succeeded) and real failures
  - Automatic recovery when deployment exists despite RPC timeout
  - Clear logging of recovery attempts and final outcome
  - Also serves as fallback when DSEQ parsing fails from successful output

### Improved
- **Resilient deployment workflow**: Script continues even when RPC nodes are slow/timeout
  - Detects "timed out waiting for tx to be included in a block" errors
  - Queries blockchain to verify if deployment was actually created
  - Continues with bid selection if deployment found (avoids duplicate deployments)
  - Only reports failure if deployment truly not created after all retry attempts
  - Better cost efficiency - no wasted AKT on abandoned successful deployments

## [1.1.1] - 2025-10-19

### Changed
- **Code simplification**: Removed redundant wallet restoration calls in `main()`
  - Each method now handles its own wallet restoration via idempotent `restore_wallet()` call
  - Eliminates coordination between `main()` and methods
  - 80% reduction in wallet handling code in `main()`
  - All methods self-contained and work standalone

### Improved
- **Bid selection logging**: Significantly improved clarity and reduced noise
  - GPU preferences now logged **once** at start instead of per-bid (90% less log noise)
  - Each bid now shows **GPU model** and **country**: `GPU: a100 (US)`
  - Enhanced final selection summary includes GPU, location, and price
  - Provider addresses shortened to 20 chars for readability
  - Makes bid selection transparent and debuggable


## [1.1.0] - 2025-10-15

### Added
- **Async deployment mode**: Default behavior now returns immediately after manifest send (~2 minutes) instead of waiting for full deployment readiness (15-20 minutes)
  - New `--check-ready` command to poll deployment status
  - Three-stage status tracking: `starting` → `starting_services` → `downloading_models` → `ready`
  - Returns `ready: false` with status information for n8n workflow polling
  - Perfect for n8n integration - no more workflow timeouts or memory errors
- **Single log file per deployment**: All logs for a given DSEQ now consolidated into one file
  - Initial logs go to temporary file: `iwb-akash-deploy_YYYYMMDD_HHMMSS_temp.log`
  - Once DSEQ obtained, switches to: `iwb-akash-deploy_{DSEQ}.log`
  - Automatic log copying and cleanup of temporary files
  - If DSEQ provided at init, uses DSEQ log file immediately
- Comprehensive documentation for async deployment and n8n integration
  - `ASYNC-DEPLOYMENT-N8N-INTEGRATION.md` - Full implementation guide
  - `ASYNC-QUICK-REFERENCE.md` - Quick reference for daily use
  - `LOG-FILE-CONSOLIDATION-FIX.md` - Logging improvements details

### Changed
- **Breaking**: Default `run()` behavior now returns after manifest send without waiting for ready status
  - Use `--check-ready` to poll for deployment readiness
  - Enables non-blocking n8n workflows with full timing control
- Log file naming now includes DSEQ for easy identification

### Improved
- **Code condensation**: Reduced from 2062 lines to 2000 lines (3% reduction) through:
  - New `_error_response()` helper method eliminates repeated error dict structures
  - New `_update_deployment_metadata()` consolidates service URL and API credentials updates
  - New `_parse_dseq_from_output()` condenses DSEQ extraction logic with modern Python patterns
  - Better code organization and maintainability
  - See `CODE-CONDENSATION-SUMMARY.md` for details

### Technical Details
- Added `check_ready()` method to check services and model download status
- Modified `run()` to return immediately with `status: 'starting'`
- Enhanced state management with status progression tracking
- Log file switching with automatic history preservation
- All functionality preserved with improved efficiency

## [1.0.2] - 2025-10-15

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
