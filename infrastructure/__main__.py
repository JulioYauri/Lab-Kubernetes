import pulumi
import pulumi_azure_native as azure
import pulumi_kubernetes as k8s
import base64
from pulumi_kubernetes.apps.v1 import Deployment
from pulumi_kubernetes.core.v1 import Service, Secret, Namespace

# Configuración
config = pulumi.Config()
app_name = "taskapp"
environment = pulumi.get_stack()
location = config.get("location") or "westus"

tags = {
    "Project": app_name,
    "Environment": environment,
    "ManagedBy": "Pulumi"
}

resource_group = azure.resources.ResourceGroup(
    f"{app_name}-rg",
    location=location,
    resource_group_name=f"{app_name}-{environment}-rg",
    tags=tags
)

# ACR 
acr_registry = azure.containerregistry.Registry(
    f"{app_name}-acr",
    resource_group_name=resource_group.name,
    location=resource_group.location,
    registry_name=f"{app_name}{environment}acr",
    sku=azure.containerregistry.SkuArgs(name="Basic"),
    admin_user_enabled=True,
    tags=tags
)

acr_credentials = pulumi.Output.all(resource_group.name, acr_registry.name).apply(
    lambda args: azure.containerregistry.list_registry_credentials(
        resource_group_name=args[0],
        registry_name=args[1]
    )
)

#AKS

aks_cluster = azure.containerservice.ManagedCluster(
    f"{app_name}-aks",
    resource_group_name=resource_group.name,
    location=resource_group.location,
    dns_prefix=f"{app_name}-{environment}",
    kubernetes_version="1.32",
    
    identity=azure.containerservice.ManagedClusterIdentityArgs(
        type="SystemAssigned"
    ),
    
    agent_pool_profiles=[
        azure.containerservice.ManagedClusterAgentPoolProfileArgs(
            name="agentpool",
            count=2,
            vm_size="Standard_B2s",
            os_type="Linux",
            mode="System",
            type="VirtualMachineScaleSets",
            enable_auto_scaling=True,
            min_count=1,
            max_count=3,
        )
    ],
    
    network_profile=azure.containerservice.ContainerServiceNetworkProfileArgs(
        network_plugin="kubenet",
        load_balancer_sku="standard",
        service_cidr="10.0.0.0/16",
        dns_service_ip="10.0.0.10",
    ),
    
    tags=tags
)


# cluster_details = pulumi.Output.all(
#     resource_group.name, 
#     aks_cluster.name
# ).apply(
#     lambda args: azure.containerservice.get_managed_cluster_output(
#         resource_group_name=args[0],
#         resource_name=args[1]
#     )
# )


# acr_assignment = azure.authorization.RoleAssignment(
#     f"{app_name}-acr-pull-role",
#     principal_id=kubelet_identity,
#     principal_type="ServicePrincipal",
#     role_definition_id=pulumi.Output.concat(
#         "/subscriptions/",
#         azure.authorization.get_client_config().subscription_id,
#         "/providers/Microsoft.Authorization/roleDefinitions/7f951dda-4ed3-4680-a7ca-43fe172d538d"
#     ),
#     scope=acr_registry.id,
#     opts=pulumi.ResourceOptions(depends_on=[aks_cluster])
# )


kubeconfig = pulumi.Output.all(resource_group.name, aks_cluster.name).apply(
    lambda args: azure.containerservice.list_managed_cluster_user_credentials_output(
        resource_group_name=args[0],
        resource_name=args[1]
    )
)

kubeconfig_decoded = kubeconfig.apply(
    lambda creds: base64.b64decode(creds.kubeconfigs[0].value).decode('utf-8')
)

k8s_provider = k8s.Provider(
    f"{app_name}-k8s-provider",
    kubeconfig=kubeconfig_decoded,
    opts=pulumi.ResourceOptions(depends_on=[aks_cluster])
)

namespace = Namespace(
    f"{app_name}-namespace",
    metadata={"name": app_name},
    opts=pulumi.ResourceOptions(provider=k8s_provider)
)

mongodb_secret = Secret(
    "mongodb-secret",
    metadata={
        "name": "mongodb-secret",
        "namespace": namespace.metadata["name"]
    },
    string_data={
        "username": config.require_secret("mongodb-username"),
        "password": config.require_secret("mongodb-password")
    },
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[namespace])
)

# MONGO

mongodb_deployment = Deployment(
    "mongodb",
    metadata={
        "name": "mongodb",
        "namespace": namespace.metadata["name"],
        "labels": {"app": "mongodb"}
    },
    spec={
        "replicas": 1,
        "selector": {"match_labels": {"app": "mongodb"}},
        "template": {
            "metadata": {"labels": {"app": "mongodb"}},
            "spec": {
                "containers": [{
                    "name": "mongodb",
                    "image": "mongo:7.0",
                    "ports": [{"container_port": 27017}],
                    "env": [
                        {
                            "name": "MONGO_INITDB_ROOT_USERNAME",
                            "value_from": {
                                "secret_key_ref": {
                                    "name": "mongodb-secret",
                                    "key": "username"
                                }
                            }
                        },
                        {
                            "name": "MONGO_INITDB_ROOT_PASSWORD",
                            "value_from": {
                                "secret_key_ref": {
                                    "name": "mongodb-secret",
                                    "key": "password"
                                }
                            }
                        }
                    ],
                    "resources": {
                        "requests": {"memory": "256Mi", "cpu": "250m"},
                        "limits": {"memory": "512Mi", "cpu": "500m"}
                    },
                    "volume_mounts": [{"name": "mongodb-data", "mount_path": "/data/db"}]
                }],
                "volumes": [{"name": "mongodb-data", "empty_dir": {}}]
            }
        }
    },
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[mongodb_secret])
)

mongodb_service = Service(
    "mongo-service",
    metadata={"name": "mongo-service", "namespace": namespace.metadata["name"]},
    spec={
        "selector": {"app": "mongodb"},
        "ports": [{"port": 27017, "target_port": 27017}],
        "type": "ClusterIP"
    },
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[mongodb_deployment])
)


#BACKEND

backend_image = pulumi.Output.concat(acr_registry.login_server, "/", app_name, "-backend:latest")

backend_deployment = Deployment(
    "backend",
    metadata={
        "name": "backend",
        "namespace": namespace.metadata["name"],
        "labels": {"app": "backend"}
    },
    spec={
        "replicas": 2,
        "selector": {"match_labels": {"app": "backend"}},
        "template": {
            "metadata": {"labels": {"app": "backend"}},
            "spec": {
                "init_containers": [{
                    "name": "wait-for-mongo",
                    "image": "busybox:1.36",
                    "command": ["sh", "-c", "until nc -z mongo-service 27017; do echo waiting for mongo; sleep 2; done"]
                }],
                "containers": [{
                    "name": "backend",
                    "image": backend_image,
                    "ports": [{"container_port": 3000}],
                    "env": [
                        {"name": "MONGO_USER", "value_from": {"secret_key_ref": {"name": "mongodb-secret", "key": "username"}}},
                        {"name": "MONGO_PASS", "value_from": {"secret_key_ref": {"name": "mongodb-secret", "key": "password"}}}
                    ],
                    "resources": {
                        "requests": {"memory": "128Mi", "cpu": "100m"},
                        "limits": {"memory": "256Mi", "cpu": "200m"}
                    },
                    "startup_probe": {"http_get": {"path": "/health", "port": 3000}, "failure_threshold": 30, "period_seconds": 10},
                    "liveness_probe": {"http_get": {"path": "/health", "port": 3000}, "initial_delay_seconds": 30, "period_seconds": 10},
                    "readiness_probe": {"http_get": {"path": "/health", "port": 3000}, "initial_delay_seconds": 10, "period_seconds": 5}
                }]
            }
        }
    },

    # opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[mongodb_service, acr_assignment])
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[mongodb_service])
)

backend_service = Service(
    "backend-service",
    metadata={"name": "backend-service", "namespace": namespace.metadata["name"]},
    spec={
        "selector": {"app": "backend"},
        "ports": [{"port": 3000, "target_port": 3000}],
        "type": "LoadBalancer"
    },
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[backend_deployment])
)

#FRONTEND

frontend_image = pulumi.Output.concat(acr_registry.login_server, "/", app_name, "-frontend:latest")

frontend_deployment = Deployment(
    "frontend",
    metadata={
        "name": "frontend",
        "namespace": namespace.metadata["name"],
        "labels": {"app": "frontend"}
    },
    spec={
        "replicas": 2,
        "selector": {"match_labels": {"app": "frontend"}},
        "template": {
            "metadata": {"labels": {"app": "frontend"}},
            "spec": {
                "containers": [{
                    "name": "frontend",
                    "image": frontend_image,
                    "ports": [{"container_port": 80}],
                    "resources": {
                        "requests": {"memory": "64Mi", "cpu": "50m"},
                        "limits": {"memory": "128Mi", "cpu": "100m"}
                    },
                    "liveness_probe": {"http_get": {"path": "/", "port": 80}, "initial_delay_seconds": 10, "period_seconds": 10}
                }]
            }
        }
    },
    # opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[backend_service, acr_assignment])
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[backend_service])
)

frontend_service = Service(
    "frontend-service",
    metadata={"name": "frontend-service", "namespace": namespace.metadata["name"]},
    spec={
        "selector": {"app": "frontend"},
        "ports": [{"port": 80, "target_port": 80}],
        "type": "LoadBalancer"
    },
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[frontend_deployment])
)

pulumi.export("resource_group_name", resource_group.name)
pulumi.export("location", resource_group.location)
pulumi.export("acr_login_server", acr_registry.login_server)
pulumi.export("acr_name", acr_registry.name)
pulumi.export("acr_username", acr_credentials.apply(lambda c: c.username))
pulumi.export("acr_password", pulumi.Output.secret(acr_credentials.apply(lambda c: c.passwords[0].value)))
pulumi.export("aks_cluster_name", aks_cluster.name)
pulumi.export("aks_fqdn", aks_cluster.fqdn)
pulumi.export("kubeconfig", pulumi.Output.secret(kubeconfig_decoded))
# pulumi.export("kubelet_identity_id", kubelet_identity)  # Para debug
pulumi.export("backend_url", backend_service.status.apply(
    lambda s: s.load_balancer.ingress[0].ip if s and s.load_balancer and s.load_balancer.ingress and len(s.load_balancer.ingress) > 0 else "pending"
))
pulumi.export("frontend_url", frontend_service.status.apply(
    lambda s: s.load_balancer.ingress[0].ip if s and s.load_balancer and s.load_balancer.ingress and len(s.load_balancer.ingress) > 0 else "pending"
))
pulumi.export("backend_image", backend_image)
pulumi.export("frontend_image", frontend_image)