output "app_name" {
  value = juju_application.catalogue.name
}

output "endpoints" {
  value = {
    catalogue    = "catalogue",
    certificates = "certificates",
    ingress      = "ingress",
    tracing      = "tracing",
  }
}
