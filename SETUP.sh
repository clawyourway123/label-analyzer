#!/bin/bash
# ONE-COMMAND SETUP FOR LABEL ANALYZER
# Just run: bash SETUP.sh

set -e

echo "🚀 Label Analyzer Setup"
echo ""

# Install dependencies
echo "1️⃣  Installing dependencies..."
pip install -q -r requirements.txt
echo "   ✅ Dependencies installed"

# Verify imports
echo ""
echo "2️⃣  Verifying installation..."
python3 << 'EOF'
try:
    from label_analyzer_production import LabelAnalyzer
    print("   ✅ label_analyzer_production imports OK")
except Exception as e:
    print(f"   ❌ Import failed: {e}")
    exit(1)
EOF

# Create data directories
echo ""
echo "3️⃣  Creating directories..."
mkdir -p data/in data/out
echo "   ✅ Directories ready"

# Final check
echo ""
echo "4️⃣  Final verification..."
if [ -f "label_analyzer_production.py" ] && [ -f "3B_True_DPI_Production.ipynb" ]; then
    echo "   ✅ All files present"
else
    echo "   ❌ Missing files"
    exit 1
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ SETUP COMPLETE"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📖 NEXT STEPS:"
echo ""
echo "1. Place your PDF in: data/in/"
echo "2. Update notebook: ARTWORK_FILE = 'your_filename'"
echo "3. Run: jupyter notebook 3B_True_DPI_Production.ipynb"
echo ""
echo "Or use Python directly:"
echo "   from label_analyzer_production import analyze_image_file"
echo "   analyzer, parts = analyze_image_file('path/to/image.jpg', 'your-project-id')"
echo ""
