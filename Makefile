.PHONY: protos deps run submodule

# Variables
PROTO_SOURCE_DIR=falcon-protos
PROTO_BUILD_DIR=src/generated

# Find all .proto files in the proto directory and subdirectories
PROTO_SRC := $(shell find $(PROTO_SOURCE_DIR) -name "*.proto")
BETTER_PROTO_PLUGIN=$(shell find .venv -name protoc-gen-python_betterproto2\*)

# 1=true, 0=false
DOCKER_DISABLED=1
export DOCKER_DISABLED

MKDIR = mkdir -p $(1)
RM = rm -rf $(1)
SEPARATOR = /

# Commands
protos:
	$(call RM,$(PROTO_BUILD_DIR))
	$(call MKDIR,$(PROTO_BUILD_DIR))

	protoc \
    --plugin=protoc-gen-python_betterproto2=$(BETTER_PROTO_PLUGIN) \
    -I=$(PROTO_SOURCE_DIR) \
    --python_betterproto2_out=$(PROTO_BUILD_DIR) \
    $(PROTO_SRC)

# Create the directory if it doesn't exist
$(PROTO_BUILD_DIR):
	mkdir -p $(PROTO_BUILD_DIR)

deps:
	uv run sync

run:
	@if [ ! -d "$(PROTO_BUILD_DIR)" ]; then \
		echo "Protobuf build directory not found. Please run 'make proto'"; \
		exit 1; \
	fi

	uv run src/main.py

submodule:
	git submodule update --remote --merge