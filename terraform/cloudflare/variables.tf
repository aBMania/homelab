variable "zone_name" {
  description = "DNS zone managed here (DOMAIN_0 in clusterenv.yaml). Kept out of git: set TF_VAR_zone_name in .env."
  type        = string
}
