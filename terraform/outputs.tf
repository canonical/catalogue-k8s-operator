output "app_name" {
  value = juju_application.catalogue.name
}

output "provides" {
  value = {
    catalogue = "catalogue",
  }
}

output "requires" {
  value = {
    certificates = "certificates",
    ingress      = "ingress",
    logging      = "logging",
    tracing      = "tracing",
  }
}
