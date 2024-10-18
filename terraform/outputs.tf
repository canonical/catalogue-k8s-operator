output "app_name" {
  value = juju_application.catalogue.name
}

output "requires" {
  value = {
    certificates = "certificates",
    ingress      = "ingress",
    tracing      = "tracing",
  }
}

output "provides" {
  value = {
    catalogue = "catalogue",
  }
}
