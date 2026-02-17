# Label Analyzer Dashboard

Interactive web UI for visualizing label analyzer results. Runs locally on your work computer, compatible with PowerApps.

## Features

- **Real-time metrics** — DPI calibration, package size, confidence levels
- **Interactive cards** — Hover over metrics to see detailed tooltips explaining each check
- **Region analysis** — See which CLP regions pass/fail with color-coded compliance status
- **Detailed checks** — Font size, line spacing, contrast — all validated against EU CLP rules
- **Summary stats** — Quick overview of compliance rate and measurement accuracy
- **Mobile-friendly** — Responsive design works on desktop and tablet

## Quick Start

1. **Open the dashboard:**
   ```
   Open label_analyzer_dashboard.html in any web browser
   ```

2. **Live data (via Node.js backend):**
   ```bash
   # In Desktop folder, start a simple server:
   python3 -m http.server 8000
   # Then open: http://localhost:8000/label_analyzer_dashboard.html
   ```

3. **Connect to real analyzer output:**
   - Dashboard currently shows sample data
   - To wire up real results, update the `sampleData` object in the script section
   - Or call an API endpoint that returns analyzer results in the same format

## Data Format

Dashboard expects results in this structure:

```json
{
  "timestamp": "14:30:45",
  "calibration": {
    "dpi": 257,
    "is_calibrated": true,
    "dpmm": 10.14
  },
  "package_size": {
    "value_ml": 500,
    "confidence": 0.95
  },
  "regions": [
    {
      "label": "Ingredients List",
      "classification": "CLP",
      "confidence": 0.92,
      "checks": {
        "font_size": {
          "measured_mm": 2.01,
          "expected_mm": 2.0,
          "error_pct": 0.5,
          "status": "PASS",
          "detail": "2.01mm >= 1.2mm threshold ✓"
        },
        "line_distance": {
          "measured_mm": 2.41,
          "required_mm": 2.41,
          "error_pct": 0,
          "status": "PASS",
          "detail": "2.41mm = 120% of font size ✓"
        },
        "contrast": {
          "measured": "white bg, black text",
          "status": "PASS",
          "detail": "Meets EU regulation requirement ✓"
        }
      },
      "compliant": true,
      "bounding_box": {
        "x": 50,
        "y": 120,
        "width": 250,
        "height": 180
      }
    }
  ]
}
```

## Check Status Badges

- **PASS** (green) — Meets requirement
- **FAIL** (red) — Does not meet requirement
- **WARN** (orange) — Borderline, needs review
- **INFO** (blue) — Informational (no requirement)

## Hovering Over Metrics

Each metric shows a tooltip on hover explaining:
- What the value means
- How it's calculated
- Why it matters for compliance

Examples:
- Hover over "DPI" → See how DPI affects measurement accuracy
- Hover over "Font: 2.01mm" → See the EU requirement it's being validated against
- Hover over compliance badge → See the specific rule being checked

## PowerApps Integration

To embed this in PowerApps:

1. **Save as a static file** → Upload to SharePoint or Azure Blob Storage
2. **Embed via Web Component** → Use PowerApps Web component to display
3. **Dynamic data** → Connect a Power Automate flow to send analyzer results to the dashboard

Or host as a standalone web app and link from PowerApps.

## Features Coming Soon

- [ ] Live data binding from analyzer API
- [ ] Visualization of bounding boxes on label image
- [ ] Export results as PDF report
- [ ] Historical trend graphs
- [ ] Custom compliance rule editor

## File Structure

```
Desktop/
├── label_analyzer_dashboard.html    # Main dashboard UI
├── label_analyzer_production.py     # Analyzer backend
├── test_font_accuracy.py            # Test script
└── DASHBOARD_README.md              # This file
```

## Support

For questions or issues:
- Check analyzer logs: `[MEASUREMENT]`, `[COMPLIANCE]` tags
- Review IMPLEMENTATION_OPUS_DESIGNED.md for technical details
- Check latest commits for recent changes

---

**Status:** Production Ready  
**Last Updated:** 2026-02-17  
**Version:** 1.0
