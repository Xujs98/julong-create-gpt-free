DOCKER_IMAGE ?= qq1371446705/turb-gpt-free-register
TAG ?= latest
EXPECTED_GIT_BRANCH ?= main

.PHONY: check-docker-branch docker-build docker-push docker-up docker-down docker-logs macos-up macos-update

check-docker-branch:
	@test "$$(git branch --show-current)" = "$(EXPECTED_GIT_BRANCH)" || \
		(echo "Current branch must be $(EXPECTED_GIT_BRANCH)" >&2; exit 1)

docker-build: check-docker-branch
	docker build --pull $(if $(DOCKER_PLATFORM),--platform $(DOCKER_PLATFORM),) -t $(DOCKER_IMAGE):$(TAG) .

docker-push: check-docker-branch
	DOCKER_IMAGE=$(DOCKER_IMAGE) EXPECTED_GIT_BRANCH=$(EXPECTED_GIT_BRANCH) ./docker-publish.sh $(TAG)

docker-up: check-docker-branch
	mkdir -p docker-data
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f app

macos-up:
	./macos-deploy.sh

macos-update:
	./macos-deploy.sh update
