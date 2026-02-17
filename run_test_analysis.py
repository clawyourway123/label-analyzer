#!/usr/bin/env python3
"""Simple test runner for label analyzer"""

import os
import sys
import json
from pathlib import Path

# Import the analyzer
from label_analyzer_production import analyze_image_file

def main():
    # Test images directory
    test_dir = Path("/Users/clawdy/Desktop/test_labels")
    
    # Find all jpg images
    test_images = list(test_dir.glob("*.jpg"))
    
    if not test_images:
        print("No test images found!")
        return
    
    print(f"Found {len(test_images)} test images")
    print("=" * 80)
    
    # Get GCP project ID from environment or use dummy
    project_id = os.environ.get("GCP_PROJECT_ID", "gemini-analysis-447816")
    
    # Analyze each image
    for img_path in test_images[:2]:  # Limit to first 2 to avoid excessive API calls
        print(f"\n\n{'='*80}")
        print(f"Analyzing: {img_path.name}")
        print(f"{'='*80}\n")
        
        try:
            analyzer, parts = analyze_image_file(str(img_path), project_id)
            
            print(f"\n✅ Analysis complete: {len(parts)} parts detected")
            
            for i, part in enumerate(parts, 1):
                print(f"\n--- Part {i}: {part.classification} ---")
                print(f"  Confidence: {part.confidence:.2f}")
                print(f"  Bounding box: {part.bbox}")
                if hasattr(part, 'compliance_check') and part.compliance_check:
                    comp = part.compliance_check
                    print(f"  Compliance: {comp.get('overall_compliance', 'N/A')}")
                    if 'violations' in comp:
                        print(f"  Violations: {len(comp['violations'])}")
            
            # Save detailed results
            output_file = test_dir / f"{img_path.stem}_analysis.json"
            with open(output_file, 'w') as f:
                json.dump([p.__dict__ for p in parts], f, indent=2, default=str)
            print(f"\n📁 Detailed results saved to: {output_file.name}")
            
        except Exception as e:
            print(f"❌ Error analyzing {img_path.name}: {e}")
            import traceback
            traceback.print_exc()
    
    print("\n" + "=" * 80)
    print("Test analysis complete!")

if __name__ == "__main__":
    main()
