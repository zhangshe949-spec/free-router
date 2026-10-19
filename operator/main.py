"""FreeRouter Operator entry point."""
import kopf
import kubernetes
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main():
    # Load kube config (in-cluster or local)
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()

    kopf.run(
        "freerouter.io",
        auto_create_crd=False,  # We apply CRD separately
        verbose=True,
    )


if __name__ == "__main__":
    main()