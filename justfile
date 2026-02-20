rock_name := "catalogue"
rock_version := "0.15.0"
rock_file := rock_name + "_" + rock_version + "_amd64.rock"
charm_file := "catalogue-k8s_ubuntu@24.04-amd64.charm"
dev_image := "ghcr.io/canonical/catalogue-k8s-operator:dev"

# Build the rock using rockcraft
build-rock:
    cd workload && rockcraft pack

# Build the charm using charmcraft
build-charm:
    cd charm && charmcraft pack

# Build both the charm and the rock, placing artifacts in the project root
build-all: build-rock build-charm
    cp workload/{{ rock_file }} ./
    cp charm/{{ charm_file }} ./

# Build the rock and push it to ghcr.io as a dev image
update-dev-rock: build-rock
    rockcraft.skopeo --insecure-policy copy oci-archive:workload/{{ rock_file }} docker://{{ dev_image }}
