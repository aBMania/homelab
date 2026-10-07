# Apex A record. Its IP is kept up to date by the cloudflareddns app in the cluster
# (network/cloudflareddns), so Terraform only owns the record, never its content.
resource "cloudflare_dns_record" "apex" {
  zone_id = data.cloudflare_zone.main.zone_id
  name    = var.zone_name
  type    = "A"
  content = "192.0.2.1" # placeholder, ignored (see lifecycle)
  ttl     = 1
  proxied = false

  lifecycle {
    ignore_changes = [content]
  }
}

# Every <app>.${var.zone_name} resolves to the apex, so new ingresses need no DNS change.
resource "cloudflare_dns_record" "wildcard" {
  zone_id = data.cloudflare_zone.main.zone_id
  name    = "*.${var.zone_name}"
  type    = "CNAME"
  content = var.zone_name
  ttl     = 1
  proxied = false
  comment = "Managed by Terraform (homelab repo)"
}

resource "cloudflare_dns_record" "www" {
  zone_id = data.cloudflare_zone.main.zone_id
  name    = "www.${var.zone_name}"
  type    = "CNAME"
  content = var.zone_name
  ttl     = 1
  proxied = false
}

# Email forwarding provided by the registrar.
locals {
  mx = {
    eforward1 = 10
    eforward2 = 10
    eforward3 = 10
    eforward4 = 15
    eforward5 = 20
  }
}

resource "cloudflare_dns_record" "mx" {
  for_each = local.mx

  zone_id  = data.cloudflare_zone.main.zone_id
  name     = var.zone_name
  type     = "MX"
  content  = "${each.key}.registrar-servers.com"
  priority = each.value
  ttl      = 1
}

resource "cloudflare_dns_record" "spf" {
  zone_id = data.cloudflare_zone.main.zone_id
  name    = var.zone_name
  type    = "TXT"
  content = "\"v=spf1 include:spf.efwd.registrar-servers.com ~all\""
  ttl     = 1
}
