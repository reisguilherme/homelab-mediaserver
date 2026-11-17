.PHONY: validate kustomize test-abyss

validate:
	node scripts/validate.mjs
	$(MAKE) test-abyss

kustomize:
	kubectl kustomize kubernetes > /dev/null

test-abyss:
	node docker/jellyfin-abyss/test-defaults.mjs
	node docker/jellyfin-abyss/test-runtime.mjs docker/jellyfin-abyss/runtime
