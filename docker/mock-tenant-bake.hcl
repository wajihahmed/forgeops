variable "CONFIG_LOADER_FROM_IMAGE" {
  default = null
}

variable "ESV_SHIM_FROM_IMAGE" {
  default = null
}

group "base-extra" {
  targets = [
    "ds-proxy",
    "rcs-agent",
    "git-server",
    "config-loader",
    "esv-shim",
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

target "esv-shim" {
  inherits = ["base"]

  context = "./esv-shim"
  dockerfile = "Dockerfile"
  args = {
    FROM_IMAGE = ESV_SHIM_FROM_IMAGE
  }

  tags = "${tags("${REGISTRY}", "${REPOSITORY}", "esv-shim", "${BUILD_TAG}")}"
  cache-to = ["mode=max,type=registry,ref=${CACHE_REGISTRY}/${CACHE_REPOSITORY}/esv-shim:build-cache"]
  cache-from = ["type=registry,ref=${CACHE_REGISTRY}/${CACHE_REPOSITORY}/esv-shim:build-cache"]
}
