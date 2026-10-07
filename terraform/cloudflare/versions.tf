terraform {
  required_version = ">= 1.16"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.27"
    }
  }
}

# Auth: the provider reads CLOUDFLARE_API_TOKEN from the environment (.env via mise).
provider "cloudflare" {}
