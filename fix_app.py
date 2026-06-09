with open("/root/rawos/rawos/api/app.py", "r") as f:
    content = f.read()

old1 = "from rawos.api.admin_routes    import router as admin_router"
new1 = old1 + "\nfrom rawos.api.billing_routes  import router as billing_router"
content = content.replace(old1, new1, 1)

old2 = "app.include_router(admin_router,              tags=[\"admin\"])"
new2 = old2 + "\napp.include_router(billing_router,            tags=[\"billing\"])"
content = content.replace(old2, new2, 1)

with open("/root/rawos/rawos/api/app.py", "w") as f:
    f.write(content)
print("app.py patched")
