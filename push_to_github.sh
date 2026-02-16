#!/bin/bash
# Push label analyzer improvements to GitHub

REPO_DIR="/Users/clawdy/Desktop"
REPO_NAME="label-analyzer"
GITHUB_USER="clawyourway123"

cd "$REPO_DIR"

# Check if remote is set
if ! git remote get-url origin &>/dev/null; then
    echo "📌 No remote set. Creating one..."
    # This would need to be set up manually or via GitHub CLI
    # For now, just show the commit
    echo "✓ Local commits ready:"
    git log --oneline | head -5
    echo ""
    echo "To push to GitHub:"
    echo "  git remote add origin https://github.com/$GITHUB_USER/$REPO_NAME.git"
    echo "  git branch -M main"
    echo "  git push -u origin main"
else
    echo "✓ Pushing to GitHub..."
    git push origin main
    echo "✓ Push complete"
fi
