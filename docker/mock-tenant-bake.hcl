variable "CONFIG_LOADER_FROM_IMAGE" {
  default = null
}

variable "TENANT_SHIM_FROM_IMAGE" {
  default = null
}

group "base-extra" {
  targets = [
    "ds-proxy",
    "rcs-agent",
    "git-server",
    "config-loader",
    "tenant-shim",
  ]
}

target "config-loader" {
  inherits = ["base"]

  context = "./config-loader"
  dockerfile = "Dockerfile"
  args = {
    FROM_IMAGE = CONFIG_LOADER_FROM_IMAGE
  }

  tags = "${tags("${REGISTRY}", "${REPOSITORY}", "config-loader", "${BUILD_TAG}")}"
  cache-to = ["mode=max,type=registry,ref=${CACHE_REGISTRY}/${CACHE_REPOSITORY}/config-loader:build-cache"]
  cache-from = ["type=registry,ref=${CACHE_REGISTRY}/${CACHE_REPOSITORY}/config-loader:build-cache"]
}

target "tenant-shim" {
  inherits = ["base"]

  context = "./tenant-shim"
  dockerfile = "Dockerfile"
  args = {
    FROM_IMAGE = TENANT_SHIM_FROM_IMAGE
  }

  tags = "${tags("${REGISTRY}", "${REPOSITORY}", "tenant-shim", "${BUILD_TAG}")}"
  cache-to = ["mode=max,type=registry,ref=${CACHE_REGISTRY}/${CACHE_REPOSITORY}/tenant-shim:build-cache"]
  cache-from = ["type=registry,ref=${CACHE_REGISTRY}/${CACHE_REPOSITORY}/tenant-shim:build-cache"]
}
