"""Create a balanced 220-question sample (20 per DB), avoiding overlap with existing 110-sample."""
import json, random

random.seed(42)

dev_path = 'bird_bench/dev/dev_20240627/dev.json'
with open(dev_path) as f:
    dev = json.load(f)

# Load existing 110-sample indices
with open('bird_bench/results/sample_indices.json') as f:
    existing = set(json.load(f))

# Group questions by db_id
from collections import defaultdict
db_groups = defaultdict(list)
for i, q in enumerate(dev):
    db_groups[q['db_id']].append(i)

# Pick 20 per DB, avoiding existing indices
new_indices = []
for db, indices in sorted(db_groups.items()):
    available = [i for i in indices if i not in existing]
    # Pick 20 from available (or all if <20)
    pick = min(20, len(available))
    selected = sorted(random.sample(available, pick))
    new_indices.extend(selected)
    existing_in_sample = len([i for i in indices if i in existing])
    print(f"  {db:30} {len(indices):>4} total, {existing_in_sample:>3} already in sample, picking {pick:>2} new → {existing_in_sample + pick:>2} total")

print(f"\nTotal new sample: {len(new_indices)} questions")
print(f"Total with existing: {len(new_indices) + 110}")

# Save
output_path = 'bird_bench/results/sample_220_indices.json'
with open(output_path, 'w') as f:
    json.dump(new_indices, f)
print(f"\nSaved to {output_path}")
