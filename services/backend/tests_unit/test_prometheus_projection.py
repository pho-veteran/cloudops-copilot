from cloudops.features.investigations.storage import _bounded


def test_metric_series_are_not_limited_by_the_kubernetes_pod_budget():
    payload = {
        "series": [
            {
                "uid": f"series-{index}",
                "name": "Request rate",
                "namespace": "demo",
                "workload_uid": "uid-1",
                "pod": f"checkout-{index:02}",
                "unit": "requests/s",
                "samples": [[index, index]],
            }
            for index in range(12)
        ]
    }
    budget = {
        "pods_seen": set(),
        "containers_seen": set(),
        "samples": 20000,
        "series": 25,
        "sample_gaps": False,
    }
    cleaned, truncated = _bounded(payload, "series", 256 * 1024, budget, "prometheus.request_rate")
    assert not truncated
    assert len(cleaned["series"]) == 12
    assert all(item for item in cleaned["series"])
