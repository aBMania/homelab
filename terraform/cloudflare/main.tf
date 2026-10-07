data "cloudflare_zone" "main" {
  filter = {
    name = var.zone_name
  }
}
