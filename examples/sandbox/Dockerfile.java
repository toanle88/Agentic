FROM eclipse-temurin:21.0.5_11-jdk-jammy

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Add Maven/Gradle, project-specific libraries, and provider CLIs here.
# COPY .mvn/ .mvn/
# COPY mvnw pom.xml ./
# RUN ./mvnw -B -DskipTests dependency:go-offline
WORKDIR /workspace
