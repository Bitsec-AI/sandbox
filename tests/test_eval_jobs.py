#!/usr/bin/env python3
"""
Simple test script to run eval_jobs on a single project without spinning up containers.
This script tests the scoring functionality using existing reports.
"""

import os
import sys
import json
from pathlib import Path

# Add the project root to the Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from validator.manager import SandboxManager

def main():
    print("Testing eval_jobs function on a single project...")
    
    # Initialize the sandbox manager
    # Note: This will still try to build images and init proxy, but we'll skip the actual job processing
    try:
        m = SandboxManager()
        print("✓ SandboxManager initialized successfully")
    except Exception as e:
        print(f"✗ Error initializing SandboxManager: {e}")
        return 1
    
    # Set the reports directory to the existing job reports
    reports_dir = os.path.join(m.curr_dir, 'jobs', 'job_local', 'reports')
    
    if not os.path.exists(reports_dir):
        print(f"✗ Reports directory not found: {reports_dir}")
        return 1
    
    print(f"✓ Using reports directory: {reports_dir}")
    
    # Check if the specific project report exists
    project_id = "cantina_smart-contract-audit-of-tn-contracts_2025_08"
    project_report_path = os.path.join(reports_dir, project_id, "report.json")
    
    if not os.path.exists(project_report_path):
        print(f"✗ Project report not found: {project_report_path}")
        return 1
    
    print(f"✓ Found project report: {project_report_path}")
    
    # Load and display basic info about the report
    try:
        with open(project_report_path, 'r') as f:
            report_data = json.load(f)
        
        print(f"✓ Report loaded successfully")
        print(f"  - Success: {report_data.get('success', 'N/A')}")
        print(f"  - Findings count: {len(report_data.get('findings', []))}")
        
        if report_data.get('success', False) and report_data.get('findings'):
            print(f"  - First finding title: {report_data['findings'][0].get('title', 'N/A')[:80]}...")
        
    except Exception as e:
        print(f"✗ Error loading report: {e}")
        return 1
    
    # Run the evaluation
    print(f"\n🚀 Running eval_jobs for project: {project_id}")
    print("=" * 60)
    
    try:
        score = m.eval_jobs(reports_dir, project_id=project_id)
        
        print("\n" + "=" * 60)
        print("📊 SCORING RESULTS:")
        print("=" * 60)
        
        if score:
            for project, result in score.items():
                print(f"\nProject: {project}")
                print(f"Status: {result.get('status', 'unknown')}")
                
                if result.get('status') == 'scored':
                    scoring_result = result.get('report', {})
                    print(f"  - Total Expected: {scoring_result.get('total_expected', 0)}")
                    print(f"  - Total Found: {scoring_result.get('total_found', 0)}")
                    print(f"  - True Positives: {scoring_result.get('true_positives', 0)}")
                    print(f"  - False Negatives: {scoring_result.get('false_negatives', 0)}")
                    print(f"  - False Positives: {scoring_result.get('false_positives', 0)}")
                    print(f"  - Detection Rate: {scoring_result.get('detection_rate', 0):.2%}")
                    print(f"  - Precision: {scoring_result.get('precision', 0):.2%}")
                    print(f"  - F1 Score: {scoring_result.get('f1_score', 0):.2%}")
                elif result.get('status') == 'failed':
                    print(f"  - Error: {result.get('error', 'Unknown error')}")
                elif result.get('status') == 'no_findings':
                    print(f"  - Message: {result.get('message', 'No findings')}")
                elif result.get('status') == 'no_benchmark':
                    print(f"  - Message: {result.get('message', 'No benchmark data')}")
                elif result.get('status') == 'error':
                    print(f"  - Error: {result.get('error', 'Unknown error')}")
        else:
            print("No scoring results returned")
        
        print("\n✅ eval_jobs test completed successfully!")
        return 0
        
    except Exception as e:
        print(f"\n❌ Error running eval_jobs: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
