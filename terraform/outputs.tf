output "app_name" {
  value = juju_application.catalogue.name
}

output "grafana_source_endpoint" {
  description = "Name of the endpoint used by Catalogue to create a datasource in Grafana."
  value       = "grafana-source"
}

output "ingress_endpoint" {
  description = "Name of the endpoint used by Catalogue for the ingress configuration."
  value       = "ingress"
}

output "catalogue_endpoint" {
  value       = "catalogue"
}
