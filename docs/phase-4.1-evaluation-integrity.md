# Phase 4.1 — Evaluation Integrity & Editorial Review

## Mathematical Specification

### Candidate-Reference Matching

**Algorithm**: Hungarian algorithm (O(n^3)) for maximum-weight bipartite matching with lexicographic objective.

**Objective**:
1. Maximize cardinality (number of valid matches)
2. Among max cardinality, maximize total IoU
3. Deterministic tie-breaking by candidate/ref indices

**Lexicographic formulation**: Each valid edge (IoU >= threshold) has weight `big_m + iou` where `big_m = 1e6`. Invalid edges have weight 0 (effectively forbidden). The Hungarian algorithm maximizes total weight, which first maximizes cardinality (each match adds 1e6) then maximizes total IoU.

**IoU (Intersection over Union)**:
```
IoU(A, B) = |A ∩ B| / |A ∪ B|
           = (min(A_end, B_end) - max(A_start, B_start)) / ((A_end - A_start) + (B_end - B_start) - intersection)
```

**Threshold**: Default 0.5. Valid edges require IoU >= threshold.

**Output**: matches (with IoU, start/end/duration errors), unmatched candidates, unmatched references, TP/FP/FN counts, algorithm identifier.

### Detection Metrics

```
TP = number of matched pairs
FP = number of unmatched candidates
FN = number of unmatched references

Precision = TP / (TP + FP)     [0 if TP+FP = 0]
Recall    = TP / (TP + FN)     [0 if TP+FN = 0]
F1        = 2*P*R / (P + R)    [0 if P+R = 0]
```

### Ranking Metrics

**Precision@K**: (relevant predictions in first K) / K
- Relevant = candidate matches a reference at the given threshold
- K = min(k, len(candidates))

**Recall@K**: (unique relevant references in first K) / (total relevant references)
- Duplicate candidates do not increase recovered reference count

**NDCG@K**: Normalized Discounted Cumulative Gain
- DCG@K = Σ (relevance[i] / log2(i+2)) for i in 0..K-1
- IDCG@K = DCG of ideal ranking (sorted by relevance descending)
- NDCG@K = DCG@K / IDCG@K [0 if IDCG = 0]
- Supports graded relevance (human importance 1-5)

**Average Precision**: Mean of precision values at each relevant position
- AP = (1/R) * Σ (precision at position i) for each relevant i
- R = total number of relevant candidates

### Boundary Metrics

- Start error = |candidate_start - reference_start| (seconds)
- End error = |candidate_end - reference_end| (seconds)
- Duration error = |candidate_duration - reference_duration| (seconds)
- Temporal IoU = intersection / union
- Boundary tolerance = fraction of matches with both errors <= tolerance

### Diversity Metrics

- Max pairwise IoU, mean pairwise IoU
- Duplicate count (IoU > 0.8)
- Temporal spread (std of start times)
- Concentration ratio (max candidates in any 20% section / total)
- Unique moments (candidates with no IoU > 0.3 with others)

### Reviewer Agreement

- **Boundary agreement**: Mean IoU, mean start/end/duration differences across annotator pairs
- **Importance agreement**: Mean importance difference, exact agreement rate
- **Category agreement**: Mean Jaccard similarity of category sets

### Experimental Ranking

**Terminology**: `editorial_priority_score` (replaces `viral_score` as deprecated alias)
- Machine-estimated ranking heuristic, NOT predictive of views/virality
- Scale: 1-10

**Adjustments** (isolated, not default):
1. Boundary penalty (-1.5 for mid-sentence start/end)
2. Context bonus (+0 to +1.0 for complete sentences)
3. Silence penalty (-0 to -2.0 for high silence ratio)
4. Diversity bonus (+0.5 for underrepresented sections)
5. Loudness normalization (-0 to -0.6 for excessive burst)

### Dataset Leakage Prevention

- Split by video, not by candidate
- One video cannot appear in multiple partitions
- Insufficient data returns explicit status
- No tuning on held-out test set

### API Endpoints

- `POST /api/benchmarks/annotations` — Save interesting moment
- `GET /api/benchmarks/annotations/{video_id}` — Load annotations
- `POST /api/benchmarks/reviews` — Save candidate review
- `GET /api/benchmarks/reviews/{video_id}` — Load reviews

### Known Limitations

1. No real annotated videos available
2. Frontend review interface is backend-only (JSON persistence)
3. Experimental ranking not validated on real data
4. No cache invalidation for benchmark results
5. No checkpoint/resume for long evaluations
6. Reviewer agreement requires 2+ annotators (returns insufficient status otherwise)
