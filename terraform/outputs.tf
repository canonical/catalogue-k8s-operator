output "app_name" {
  value = juju_application.catalogue.name
}

output "endpoints" {
  value = {
    # Requires
    certificates = "certificates",
    ingress      = "ingress",
    tracing      = "tracing",
    # Provides
    catalogue = "catalogue",
  }
}
