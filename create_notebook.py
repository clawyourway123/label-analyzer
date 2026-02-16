import json

cells = []

# Title & Overview
cells.append({
    "cell_type": "markdown",
    "metadata": {},
    "source": [
        "# Label Analyzer - Production CLP Compliance Checker\n",
        "\n",
        "**Two-Layer Deterministic Validation System**\n",
        "\n",
        "## Architecture\n",
        "\n",
        "**Stage 0: DPI Calibration**\n",
        "- Auto-detects measurement lines on label\n",
        "- Calculates true DPI for accurate font size measurements\n",
        "- Falls back to 300 DPI if no line found\n",
        "\n",
        "**Stage 1: Region Detection**\n",
        "- Identifies CLP (regulatory) vs Non-CLP (marketing) sections\n",
        "- Content types: Ingredients, Hazard Symbols, Warnings, Usage Instructions, etc.\n",
        "- Confidence scoring via ensemble (model + geometric + refinement agreement)\n",
        "\n",
        "**Stage 2: Boundary Refinement**\n",
        "- Refines region boundaries from rough to precise\n",
        "- Detects irregular (non-rectangular) shapes\n",
        "- Supports polygons, rectangles, any shape\n",
        "\n",
        "**Stage 3: CLP Compliance Measurement & Validation**\n",
        "- **Layer 1 (Gemini)**: Measures font size (mm), line distance (mm), contrast\n",
        "- **Layer 2 (Local)**: Applies deterministic rules (100% reproducible)\n",
        "  - Font size ≥ 1.2-1.8mm depending on package size\n",
        "  - Line distance ≥ 120% of font size\n",
        "  - White background + black text\n",
        "\n",
        "**Stage 4: Filtering & Confidence Thresholding**\n",
        "- Filters low-confidence detections\n",
        "- Flags borderline/uncertain results for human review\n",
        "- Returns only high-confidence, actionable results\n",
        "\n",
        "## Output\n",
        "\n",
        "✅ Compliance verdict (PASS/FAIL) per CLP region\n",
        "✅ Measurement confidence (0-1)\n",
        "✅ Human review flags (uncertain or borderline)\n",
        "✅ Structured JSON for portfolio/database updates\n",
        "✅ Visualization with detected regions marked"
    ]
})

# Setup
cells.append({
    "cell_type": "markdown",
    "metadata": {},
    "source": ["## Installation & Setup"]
})

cells.append({
    "cell_type": "code",
    "execution_count": None,
    "metadata": {},
    "outputs": [],
    "source": [
        "# Install dependencies\n",
        "!pip install -q google-genai pillow pymupdf pydantic python-dotenv"
    ]
})

cells.append({
    "cell_type": "code",
    "execution_count": None,
    "metadata": {},
    "outputs": [],
    "source": [
        "import sys\n",
        "import os\n",
        "from pathlib import Path\n",
        "sys.path.insert(0, str(Path.cwd()))\n",
        "\n",
        "from label_analyzer_production import (\n",
        "    LabelAnalyzer,\n",
        "    PartClassification,\n",
        "    image_to_base64,\n",
        "    pdf_to_image,\n",
        "    analyze_image_file\n",
        ")\n",
        "\n",
        "from PIL import Image as PIL_Image\n",
        "import json\n",
        "from datetime import datetime\n",
        "\n",
        "print('✅ All imports successful')"
    ]
})

cells.append({
    "cell_type": "code",
    "execution_count": None,
    "metadata": {},
    "outputs": [],
    "source": [
        "# Configuration\n",
        "PROJECT_ID = 'your-gcp-project-id'  # REQUIRED: Update with your GCP project\n",
        "DPI = 300  # Will be auto-calibrated if label has measurement line\n",
        "PACKAGE_SIZE_ML = 500  # Used for font size thresholds (500ml, 3000ml, etc.)\n",
        "\n",
        "# Create data directories\n",
        "os.makedirs('data/in', exist_ok=True)\n",
        "os.makedirs('data/out', exist_ok=True)\n",
        "\n",
        "print(f'✅ Configuration:')\n",
        "print(f'   Project ID: {PROJECT_ID}')\n",
        "print(f'   Default DPI: {DPI}')\n",
        "print(f'   Package size: {PACKAGE_SIZE_ML}ml')\n",
        "print(f'   Input path: ./data/in/')\n",
        "print(f'   Output path: ./data/out/')"
    ]
})

# Stage 0: Load Image
cells.append({
    "cell_type": "markdown",
    "metadata": {},
    "source": ["## Stage 0: Load Label Image"]
})

cells.append({
    "cell_type": "code",
    "execution_count": None,
    "metadata": {},
    "outputs": [],
    "source": [
        "# Path to your label PDF or image\n",
        "# Place files in ./data/in/ and update path here\n",
        "IMAGE_PATH = 'data/in/your_label.pdf'  # Change to your file\n",
        "\n",
        "if not os.path.exists(IMAGE_PATH):\n",
        "    print(f'❌ File not found: {IMAGE_PATH}')\n",
        "    print(f'Place your label PDF/image in data/in/ and update IMAGE_PATH')\n",
        "else:\n",
        "    print(f'✅ File ready: {IMAGE_PATH}')"
    ]
})

cells.append({
    "cell_type": "code",
    "execution_count": None,
    "metadata": {},
    "outputs": [],
    "source": [
        "# Load image (PDF or JPG)\n",
        "if IMAGE_PATH.lower().endswith('.pdf'):\n",
        "    img = pdf_to_image(IMAGE_PATH, dpi=DPI)\n",
        "    print(f'✅ PDF converted: {img.size[0]}×{img.size[1]} pixels')\n",
        "else:\n",
        "    img = PIL_Image.open(IMAGE_PATH)\n",
        "    print(f'✅ Image loaded: {img.size[0]}×{img.size[1]} pixels')\n",
        "\n",
        "# Display at reduced size for readability\n",
        "display(img.resize((img.width // 3, img.height // 3)))"
    ]
})

# Stage 1: Analyze
cells.append({
    "cell_type": "markdown",
    "metadata": {},
    "source": ["## Stage 1-4: Full Analysis Pipeline"]
})

cells.append({
    "cell_type": "code",
    "execution_count": None,
    "metadata": {},
    "outputs": [],
    "source": [
        "# Authenticate with Google Cloud (required once per session)\n",
        "!gcloud auth application-default login"
    ]
})

cells.append({
    "cell_type": "code",
    "execution_count": None,
    "metadata": {},
    "outputs": [],
    "source": [
        "# Initialize analyzer\n",
        "analyzer = LabelAnalyzer(project_id=PROJECT_ID, dpi=DPI)\n",
        "print(f'✅ Analyzer initialized')\n",
        "print(f'   DPI setting: {DPI}')\n",
        "print(f'   Project: {PROJECT_ID}')"
    ]
})

cells.append({
    "cell_type": "code",
    "execution_count": None,
    "metadata": {},
    "outputs": [],
    "source": [
        "# Convert image to base64 for Gemini API\n",
        "image_data = image_to_base64(img)\n",
        "print(f'✅ Image encoded for Gemini')"
    ]
})

cells.append({
    "cell_type": "code",
    "execution_count": None,
    "metadata": {},
    "outputs": [],
    "source": [
        "# Run full analysis pipeline\n",
        "# Takes ~30-60 seconds per image\n",
        "print('⏳ Running analysis...')\n",
        "print('   Stage 0: Calibrating DPI')\n",
        "print('   Stage 1: Detecting regions (CLP vs Non-CLP)')\n",
        "print('   Stage 2: Refining boundaries')\n",
        "print('   Stage 3: Measuring compliance metrics (Gemini)')\n",
        "print('   Stage 4: Applying rules & filtering')\n",
        "print()\n",
        "\n",
        "detected_parts = analyzer.analyze(img, image_data)\n",
        "\n",
        "print(f'\\n✅ Analysis complete')\n",
        "print(f'   Total regions detected: {len(detected_parts)}')"
    ]
})

# Results
cells.append({
    "cell_type": "markdown",
    "metadata": {},
    "source": ["## Results Summary"]
})

cells.append({
    "cell_type": "code",
    "execution_count": None,
    "metadata": {},
    "outputs": [],
    "source": [
        "# Classification & compliance summary\n",
        "clp_parts = [p for p in detected_parts if p.classification == PartClassification.CLP]\n",
        "non_clp = [p for p in detected_parts if p.classification == PartClassification.NON_CLP]\n",
        "compliant = [p for p in clp_parts if p.is_compliant()]\n",
        "non_compliant = [p for p in clp_parts if not p.is_compliant()]\n",
        "review_flagged = [p for p in clp_parts if p.needs_human_review()]\n",
        "\n",
        "print('\\n' + '='*80)\n",
        "print('DETECTION SUMMARY')\n",
        "print('='*80)\n",
        "print(f'\\nTotal regions: {len(detected_parts)}')\n",
        "print(f'  • CLP (regulatory): {len(clp_parts)}')\n",
        "print(f'  • Non-CLP (marketing): {len(non_clp)}')\n",
        "\n",
        "print(f'\\nCLP Compliance Status:')\n",
        "print(f'  ✓ Compliant: {len(compliant)}')\n",
        "print(f'  ✗ Non-compliant: {len(non_compliant)}')\n",
        "if review_flagged:\n",
        "    print(f'  ⚠️  Needs human review: {len(review_flagged)} (uncertain or borderline)')\n",
        "print()"
    ]
})

cells.append({
    "cell_type": "code",
    "execution_count": None,
    "metadata": {},
    "outputs": [],
    "source": [
        "# Detailed breakdown table\n",
        "print('DETAILED RESULTS')\n",
        "print('='*80)\n",
        "print(f'{'Region':<30} {'Type':<15} {'Status':<15} {'Confidence':<12}')\n",
        "print('-'*80)\n",
        "\n",
        "for part in clp_parts:\n",
        "    status = 'PASS ✓' if part.is_compliant() else 'FAIL ✗'\n",
        "    if part.needs_human_review():\n",
        "        status += ' (REVIEW)'\n",
        "    \n",
        "    conf = part.compliance_check.get('measurement_confidence', 0) if part.compliance_check else 0\n",
        "    content_type = part.content_type or 'Unknown'\n",
        "    \n",
        "    print(f'{part.label:<30} {content_type:<15} {status:<15} {conf:.0%}        ')"
    ]
})

cells.append({
    "cell_type": "markdown",
    "metadata": {},
    "source": ["## Detailed Rule Analysis"]
})

cells.append({
    "cell_type": "code",
    "execution_count": None,
    "metadata": {},
    "outputs": [],
    "source": [
        "# Show full compliance details for each CLP region\n",
        "for i, part in enumerate(clp_parts, 1):\n",
        "    if not part.compliance_check:\n",
        "        continue\n",
        "    \n",
        "    print(f'\\n{'='*80}')\n",
        "    print(f'CLP Region {i}: {part.label}')\n",
        "    print(f'Content: {part.content_type}')\n",
        "    print(f'{'='*80}')\n",
        "    \n",
        "    # Show measurements from Gemini\n",
        "    measurements = part.compliance_check.get('measurements', {})\n",
        "    if measurements:\n",
        "        print(f'\\n📏 MEASUREMENTS (Gemini)')\n",
        "        print(f'  Font size: {measurements.get(\"font_size_mm\", 0):.2f} mm ({measurements.get(\"font_size_pixels\", 0):.0f} px)')\n",
        "        print(f'  Line distance: {measurements.get(\"line_distance_mm\", 0):.2f} mm ({measurements.get(\"line_distance_pixels\", 0):.0f} px)')\n",
        "        print(f'  Background: {measurements.get(\"background_color\")}')\n",
        "        print(f'  Text color: {measurements.get(\"text_color\")}')\n",
        "        print(f'  Contrast: {measurements.get(\"contrast_assessment\")}')\n",
        "        print(f'  Confidence: {measurements.get(\"measurement_confidence\", 0):.0%}')\n",
        "    \n",
        "    # Show rule results\n",
        "    rule_results = part.compliance_check.get('rule_results', {})\n",
        "    if rule_results:\n",
        "        print(f'\\n✓/✗ RULE VALIDATION (Local, Deterministic)')\n",
        "        \n",
        "        r1 = rule_results.get('rule_1_font_size', {})\n",
        "        print(f'\\n  Rule 1: Font Size')\n",
        "        print(f'    Status: {r1.get(\"status\")}')\n",
        "        print(f'    {r1.get(\"detail\")}')\n",
        "        \n",
        "        r2 = rule_results.get('rule_2_line_distance', {})\n",
        "        print(f'\\n  Rule 2: Line Distance')\n",
        "        print(f'    Status: {r2.get(\"status\")}')\n",
        "        print(f'    {r2.get(\"detail\")}')\n",
        "        \n",
        "        r3 = rule_results.get('rule_3_background_contrast', {})\n",
        "        print(f'\\n  Rule 3: Background & Contrast')\n",
        "        print(f'    Status: {r3.get(\"status\")}')\n",
        "        print(f'    {r3.get(\"detail\")}')\n",
        "    \n",
        "    # Overall verdict\n",
        "    overall = part.compliance_check.get('overall_compliance', 'UNKNOWN')\n",
        "    review = part.needs_human_review()\n",
        "    \n",
        "    print(f'\\n🎯 VERDICT: {overall}')\n",
        "    if review:\n",
        "        print(f'   ⚠️  HUMAN REVIEW RECOMMENDED (uncertain or borderline)')"
    ]
})

# Visualization
cells.append({
    "cell_type": "markdown",
    "metadata": {},
    "source": ["## Visualization"]
})

cells.append({
    "cell_type": "code",
    "execution_count": None,
    "metadata": {},
    "outputs": [],
    "source": [
        "# Visualize detected regions\n",
        "viz = analyzer.visualize(img)\n",
        "display(viz.resize((viz.width // 3, viz.height // 3)))\n",
        "\n",
        "# Save\n",
        "out_viz = 'data/out/labeled_regions.jpg'\n",
        "viz.save(out_viz)\n",
        "print(f'✅ Saved: {out_viz}')"
    ]
})

# Export
cells.append({
    "cell_type": "markdown",
    "metadata": {},
    "source": ["## Export Results to JSON"]
})

cells.append({
    "cell_type": "code",
    "execution_count": None,
    "metadata": {},
    "outputs": [],
    "source": [
        "# Export full compliance report\n",
        "report = {\n",
        "    'timestamp': datetime.now().isoformat(),\n",
        "    'image_file': IMAGE_PATH,\n",
        "    'summary': {\n",
        "        'total_regions': len(detected_parts),\n",
        "        'clp_regions': len(clp_parts),\n",
        "        'non_clp_regions': len(non_clp),\n",
        "        'compliant_regions': len(compliant),\n",
        "        'non_compliant_regions': len(non_compliant),\n",
        "        'needs_review': len(review_flagged)\n",
        "    },\n",
        "    'regions': []\n",
        "}\n",
        "\n",
        "for part in detected_parts:\n",
        "    report['regions'].append({\n",
        "        'classification': part.classification.value,\n",
        "        'label': part.label,\n",
        "        'content_type': part.content_type,\n",
        "        'confidence': float(part.confidence),\n",
        "        'bounds': {\n",
        "            'xmin': part.rect.xmin,\n",
        "            'ymin': part.rect.ymin,\n",
        "            'xmax': part.rect.xmax,\n",
        "            'ymax': part.rect.ymax\n",
        "        },\n",
        "        'is_compliant': part.is_compliant(),\n",
        "        'needs_review': part.needs_human_review(),\n",
        "        'compliance_details': part.compliance_check\n",
        "    })\n",
        "\n",
        "# Save\n",
        "out_json = 'data/out/compliance_report.json'\n",
        "with open(out_json, 'w') as f:\n",
        "    json.dump(report, f, indent=2)\n",
        "\n",
        "print(f'✅ Report saved: {out_json}')\n",
        "print(f'\\nReport summary:')\n",
        "print(json.dumps(report['summary'], indent=2))"
    ]
})

# Batch processing
cells.append({
    "cell_type": "markdown",
    "metadata": {},
    "source": ["## Batch Processing (Optional)"]
})

cells.append({
    "cell_type": "code",
    "execution_count": None,
    "metadata": {},
    "outputs": [],
    "source": [
        "from label_analyzer_production import analyze_batch\n",
        "\n",
        "# Process multiple images concurrently\n",
        "# image_files = ['data/in/label1.pdf', 'data/in/label2.pdf', 'data/in/label3.pdf']\n",
        "#\n",
        "# results = analyze_batch(\n",
        "#     image_paths=image_files,\n",
        "#     project_id=PROJECT_ID,\n",
        "#     dpi=DPI,\n",
        "#     max_workers=3  # Concurrent processing\n",
        "# )\n",
        "#\n",
        "# for result in results:\n",
        "#     print(f'{result.path}: {len(result.parts)} regions, {result.success}')"
    ]
})

# Create notebook JSON
notebook = {
    'cells': cells,
    'metadata': {
        'kernelspec': {
            'display_name': 'Python 3',
            'language': 'python',
            'name': 'python3'
        },
        'language_info': {
            'codemirror_mode': {'name': 'ipython', 'version': 3},
            'file_extension': '.py',
            'mimetype': 'text/x-python',
            'name': 'python',
            'nbconvert_exporter': 'python',
            'pygments_lexer': 'ipython3',
            'version': '3.11.0'
        }
    },
    'nbformat': 4,
    'nbformat_minor': 4
}

with open('Label_Analyzer_Quickstart.ipynb', 'w') as f:
    json.dump(notebook, f, indent=1)

print('✅ Notebook created')
