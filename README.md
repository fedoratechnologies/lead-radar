# Lead Radar

Lead Radar is a **public-data** lead discovery + scoring pipeline that:

1. Ingests signals from RSS sources
2. Extracts organizations (heuristics for now)
3. Scores organizations using a decaying rolling window
4. (Optional) Creates/updates ERPNext Leads when a threshold is crossed

This repo contains the collector code and Kubernetes manifests to run it on the FTG cluster.

