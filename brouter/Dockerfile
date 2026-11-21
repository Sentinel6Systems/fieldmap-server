# Lines along roads and trails for the map in the browser and the print tool: the BRouter
# engine (MIT, github.com/abrensch/brouter) as a small route service, at the same release as
# the Fieldmap app and with the same two profiles (car-vario: Driving, hiking-mountain: Walking),
# on the routing data of the server (data/routing, scripts/build-routing.sh). So the browser
# draws the same lines as the phones. The web server passes on route requests only (/routing/route).
FROM eclipse-temurin:17-jre

ARG BROUTER_VERSION=1.7.10
ARG BROUTER_SHA256=023fec3ba997758e8cd7ab9e1bae52e962af3f00b57683e3de86b84ffad01532

RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl unzip; \
    rm -rf /var/lib/apt/lists/*; \
    cd /tmp; \
    curl -fsSL -o brouter.zip "https://github.com/abrensch/brouter/releases/download/v${BROUTER_VERSION}/brouter-${BROUTER_VERSION}.zip"; \
    echo "${BROUTER_SHA256}  brouter.zip" | sha256sum -c -; \
    unzip -q brouter.zip; \
    mkdir -p /opt/brouter/profiles /tmp/brouter-custom; \
    mv "brouter-${BROUTER_VERSION}/brouter-${BROUTER_VERSION}-all.jar" /opt/brouter/brouter.jar; \
    for f in lookups.dat car-vario.brf hiking-mountain.brf; do mv "brouter-${BROUTER_VERSION}/profiles2/$f" /opt/brouter/profiles/; done; \
    rm -rf /tmp/brouter.zip "/tmp/brouter-${BROUTER_VERSION}"; \
    chown nobody /tmp/brouter-custom

USER nobody
# The routing files (read-only), the profiles, a folder for custom profiles (not reachable from
# outside), the port, two routes at a time, and where it listens (BIND: 0.0.0.0 in the compose
# network, 127.0.0.1 with host networking on a Pi). A route gives up after 30 s.
ENV BIND=0.0.0.0 PORT=17777 JAVA_OPTS="-Xmx256m -DmaxRunningTime=30"
CMD ["sh", "-c", "exec java $JAVA_OPTS -cp /opt/brouter/brouter.jar btools.server.RouteServer /segments /opt/brouter/profiles /tmp/brouter-custom $PORT 2 $BIND"]
