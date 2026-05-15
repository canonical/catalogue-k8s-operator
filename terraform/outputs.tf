output "app_name" {
  value = juju_application.catalogue.name
}

output "provides" {
  value = {
    catalogue        = "catalogue",
    provide_cmr_mesh = "provide-cmr-mesh",
  }
}

output "requires" {
  value = {
    certificates     = "certificates",
    ingress          = "ingress",
    require_cmr_mesh = "require-cmr-mesh",
    service_mesh     = "service-mesh",
    tracing          = "tracing",
  }
}
