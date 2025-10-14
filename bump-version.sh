#!/bin/bash
# bump-version.sh - Bump version, commit, tag, and push to GitHub
# Usage: ./bump-version.sh [major|minor|patch]

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default to patch if no argument provided
BUMP_TYPE=${1:-patch}

echo -e "${BLUE}🚀 IWB Akash Deploy Version Bump & Release${NC}"
echo -e "${BLUE}============================================${NC}"

# Validate bump type
if [[ ! "$BUMP_TYPE" =~ ^(major|minor|patch)$ ]]; then
    echo -e "${RED}❌ Error: Invalid bump type '$BUMP_TYPE'${NC}"
    echo -e "${YELLOW}Usage: $0 [major|minor|patch]${NC}"
    exit 1
fi

# Check if we're in a git repository
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    echo -e "${RED}❌ Error: Not in a git repository${NC}"
    exit 1
fi

# Check for uncommitted changes
if ! git diff-index --quiet HEAD --; then
    echo -e "${YELLOW}⚠️  Warning: You have uncommitted changes${NC}"
    read -p "Do you want to commit them first? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${BLUE}📝 Staging all changes...${NC}"
        git add -A
        read -p "Enter commit message: " COMMIT_MSG
        git commit -m "$COMMIT_MSG"
        echo -e "${GREEN}✅ Changes committed${NC}"
    else
        echo -e "${RED}❌ Please commit or stash your changes first${NC}"
        exit 1
    fi
fi

# Get current version from iwb-akash-deploy.py
CURRENT_VERSION=$(grep -oP '__version__ = "\K[^"]+' iwb-akash-deploy.py)
echo -e "${BLUE}📌 Current version: ${YELLOW}${CURRENT_VERSION}${NC}"

# Parse current version
IFS='.' read -ra VERSION_PARTS <<< "$CURRENT_VERSION"
MAJOR=${VERSION_PARTS[0]}
MINOR=${VERSION_PARTS[1]}
PATCH=${VERSION_PARTS[2]}

# Bump version based on type
case $BUMP_TYPE in
    major)
        MAJOR=$((MAJOR + 1))
        MINOR=0
        PATCH=0
        ;;
    minor)
        MINOR=$((MINOR + 1))
        PATCH=0
        ;;
    patch)
        PATCH=$((PATCH + 1))
        ;;
esac

NEW_VERSION="${MAJOR}.${MINOR}.${PATCH}"
echo -e "${BLUE}🔼 New version: ${GREEN}${NEW_VERSION}${NC}"

# Confirm before proceeding
read -p "$(echo -e ${YELLOW}Proceed with version bump to ${GREEN}${NEW_VERSION}${YELLOW}? \(y/n\) ${NC})" -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${RED}❌ Aborted${NC}"
    exit 1
fi

# Update version in iwb-akash-deploy.py
echo -e "${BLUE}📝 Updating version in iwb-akash-deploy.py...${NC}"
sed -i "s/__version__ = \".*\"/__version__ = \"${NEW_VERSION}\"/" iwb-akash-deploy.py

# Update CHANGELOG.md - move [Unreleased] to new version
TODAY=$(date +%Y-%m-%d)
echo -e "${BLUE}📝 Updating CHANGELOG.md...${NC}"

# Check if CHANGELOG has [Unreleased] section
if grep -q "## \[Unreleased\]" CHANGELOG.md; then
    # Replace [Unreleased] with new version and date
    sed -i "s/## \[Unreleased\]/## [${NEW_VERSION}] - ${TODAY}/" CHANGELOG.md
    
    # Add new [Unreleased] section at the top after the header
    sed -i "/^## \[${NEW_VERSION}\]/i \\## [Unreleased]\\n" CHANGELOG.md
else
    echo -e "${YELLOW}⚠️  Warning: No [Unreleased] section found in CHANGELOG.md${NC}"
    echo -e "${YELLOW}   You may need to update CHANGELOG.md manually${NC}"
fi

# Show changes
echo -e "\n${BLUE}📋 Changes to be committed:${NC}"
git diff iwb-akash-deploy.py CHANGELOG.md

# Commit the version bump
echo -e "\n${BLUE}💾 Committing version bump...${NC}"
git add iwb-akash-deploy.py CHANGELOG.md
git commit -m "Bump version to ${NEW_VERSION}"

# Create git tag
echo -e "${BLUE}🏷️  Creating git tag v${NEW_VERSION}...${NC}"
git tag -a "v${NEW_VERSION}" -m "Release version ${NEW_VERSION}"

# Push to GitHub
echo -e "${BLUE}⬆️  Pushing to GitHub...${NC}"
git push origin main
git push origin "v${NEW_VERSION}"

echo -e "\n${GREEN}✅ Version bump complete!${NC}"
echo -e "${GREEN}   Version: ${NEW_VERSION}${NC}"
echo -e "${GREEN}   Tag: v${NEW_VERSION}${NC}"
echo -e "${GREEN}   Pushed to: origin/main${NC}"
echo -e "\n${BLUE}🎉 Release v${NEW_VERSION} is now live on GitHub!${NC}"
